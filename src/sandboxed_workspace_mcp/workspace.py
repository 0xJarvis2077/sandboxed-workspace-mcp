"""Filesystem operations confined to one validated workspace root."""

from __future__ import annotations

import bisect
import codecs
import fnmatch
import hashlib
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .access_policy import AccessPolicy, NarrowingPathFilter
from .config import Settings
from .safe_regex import SafeRegex

TRUNCATION_MARKER = "\n\n... OUTPUT TRUNCATED ..."
_SEARCH_CHUNK_BYTES = 64 * 1024


class WorkspaceError(ValueError):
    """Raised when a workspace operation violates its contract."""


@dataclass(slots=True)
class _ScanState:
    scanned: int = 0
    skipped: int = 0
    dropped: int = 0
    exhausted: bool = False


@dataclass(frozen=True, slots=True)
class _FileState:
    sha256: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    mode: int


@dataclass(slots=True)
class _SearchBudget:
    max_bytes: int
    deadline: float
    cancellation_event: threading.Event | None
    bytes_read: int = 0
    stop_reason: str | None = None

    def should_stop(self) -> bool:
        if self.stop_reason is not None:
            return True
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            self.stop_reason = "cancelled"
            return True
        if time.monotonic() >= self.deadline:
            self.stop_reason = "timeout"
            return True
        if self.bytes_read >= self.max_bytes:
            self.stop_reason = "bytes"
            return True
        return False


@dataclass(frozen=True, slots=True)
class _DirectoryEntry:
    name: str
    path: Path
    is_directory: bool
    is_symlink: bool
    link_state: str | None = None
    link_is_directory: bool = False

    @property
    def sort_key(self) -> tuple[bool, str, str]:
        return (not self.is_directory, self.name.casefold(), self.name)


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Return text whose UTF-8 representation fits within ``max_bytes``."""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    marker = TRUNCATION_MARKER.encode("utf-8")
    if max_bytes <= len(marker):
        return marker[:max_bytes].decode("utf-8", errors="ignore")

    prefix = encoded[: max_bytes - len(marker)].decode("utf-8", errors="ignore")
    return prefix + TRUNCATION_MARKER


class Workspace:
    """Own deterministic, size-bounded filesystem access under a root."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.root
        self.policy = AccessPolicy(settings.blocked_patterns)
        self._search_capacity = threading.BoundedSemaphore(
            settings.max_concurrent_searches
        )
        self._path_locks: dict[str, threading.Lock] = {}
        self._path_locks_guard = threading.Lock()

    def is_inside(self, path: Path) -> bool:
        """Return whether a resolved path remains inside the workspace."""

        try:
            path.resolve(strict=False).relative_to(self.root)
            return True
        except (ValueError, RuntimeError, OSError):
            return False

    def safe_path(self, value: str = ".") -> Path:
        """Resolve a path and enforce both root confinement and blocked rules."""

        if "\x00" in value:
            raise WorkspaceError("NUL byte is not allowed in a path")

        supplied = Path(value or ".").expanduser()
        candidate = supplied if supplied.is_absolute() else self.root / supplied
        lexical = Path(os.path.abspath(candidate))
        try:
            lexical_relative = lexical.relative_to(self.root)
        except ValueError:
            lexical_relative = None
        if lexical_relative is not None:
            self._require_allowed(lexical_relative, value)

        try:
            resolved = candidate.resolve(strict=False)
        except (RuntimeError, OSError) as exc:
            raise WorkspaceError(f"cannot resolve path: {value}") from exc

        resolved_relative = self._relative_inside(resolved, value)
        self._require_allowed(resolved_relative, value)
        return resolved

    def relative_path(self, path: Path) -> str:
        """Render a trusted workspace path relative to the root."""

        try:
            resolved = path.resolve(strict=False)
            relative = resolved.relative_to(self.root)
        except (ValueError, RuntimeError, OSError) as exc:
            raise WorkspaceError("cannot render a path outside the workspace") from exc
        return "." if not relative.parts else relative.as_posix()

    def truncate(self, text: str) -> str:
        return truncate_utf8(text, self.settings.max_output_size)

    def project_info(self) -> str:
        writable = self.settings.allow_writes and os.access(self.root, os.W_OK)
        return (
            f"Allowed project root: {self.root}\n"
            f"Exists: {self.root.exists()}\n"
            f"Writable: {writable}\n"
            f"Mode: {'read-write' if self.settings.allow_writes else 'read-only'}\n"
            f"Blocked patterns: {len(self.settings.blocked_patterns)}\n"
            f"Scan entry budget: {self.settings.max_scan_entries}\n"
            f"Search byte budget: {self.settings.max_search_bytes}\n"
            f"Concurrent searches: {self.settings.max_concurrent_searches}"
        )

    def list_directory(self, path: str = ".") -> str:
        target = self._existing_directory(path)
        state = _ScanState()
        entries = self._scan_directory(
            target,
            state,
            keep_limit=self.settings.max_tree_entries,
            skip_ignored=False,
            strict=True,
        )

        rendered = [self._render_list_entry(entry) for entry in entries]
        self._append_scan_diagnostics(rendered, state, "listing")
        return self.truncate("\n".join(rendered) or "(empty directory)")

    def tree(self, path: str = ".", max_depth: int = 4) -> str:
        target = self._existing_directory(path)
        depth_limit = max(1, min(max_depth, self.settings.max_tree_depth))
        label = target.name or str(target)
        result = [f"{label}/"]
        state = _ScanState()
        entry_count = 0

        def visit(directory: Path, depth: int) -> bool:
            nonlocal entry_count
            if depth >= depth_limit:
                return False
            remaining = self.settings.max_tree_entries - entry_count
            if remaining <= 0:
                return True

            entries = self._scan_directory(
                directory,
                state,
                keep_limit=remaining,
                skip_ignored=True,
                strict=depth == 0,
            )
            visible: list[_DirectoryEntry] = []
            directories: list[_DirectoryEntry] = []
            for entry in entries:
                if entry.is_symlink and (
                    entry.link_state is not None or entry.link_is_directory
                ):
                    state.skipped += 1
                    continue
                visible.append(entry)
                if entry.is_directory:
                    directories.append(entry)

            indent = "  " * (depth + 1)
            for entry in visible:
                marker = "/" if entry.is_directory else ""
                result.append(f"{indent}{entry.name}{marker}")
                entry_count += 1
                if entry_count >= self.settings.max_tree_entries:
                    return True

            if state.exhausted:
                return False
            for entry in directories:
                if visit(entry.path, depth + 1):
                    return True
                if state.exhausted:
                    return False
            return False

        return_truncated = visit(target, 0)
        if return_truncated or state.dropped:
            result.append("... tree return limit reached ...")
        if state.exhausted:
            result.append("... tree scan budget exhausted ...")
        if state.skipped:
            result.append(f"... tree skipped {state.skipped} inaccessible entries ...")
        return self.truncate("\n".join(result))

    def create_directory(self, path: str) -> str:
        self._require_writable()
        target = self.safe_path(path)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f"cannot create directory: {path}: {exc}") from exc
        if not target.is_dir():
            raise WorkspaceError(f"target is not a directory: {path}")
        return f"Directory ready: {self.relative_path(target)}"

    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        target = self.safe_path(path)
        text = self._read_text(target, errors="replace")
        lines = text.splitlines(keepends=True)
        first = max(start_line, 1)

        if end_line > 0 and end_line < first:
            raise WorkspaceError("end_line is before start_line")

        selected = lines[first - 1 :] if end_line <= 0 else lines[first - 1 : end_line]
        return self.truncate("".join(selected))

    def read_file_versioned(
        self, path: str, start_line: int = 1, end_line: int = 0
    ) -> dict[str, object]:
        """Read text plus an optimistic-concurrency version token."""

        target = self.safe_path(path)
        data, state = self._read_bytes_and_state(target)
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        first = max(start_line, 1)
        if end_line > 0 and end_line < first:
            raise WorkspaceError("end_line is before start_line")
        selected = lines[first - 1 :] if end_line <= 0 else lines[first - 1 : end_line]
        return {
            "path": self.relative_path(target),
            "content": self.truncate("".join(selected)),
            "sha256": state.sha256,
            "size": state.size,
            "mtime_ns": state.mtime_ns,
        }

    def tail_file(self, path: str, line_count: int = 50) -> str:
        target = self.safe_path(path)
        count = max(1, min(line_count, 1_000))
        lines = self._read_text(target, errors="replace").splitlines()
        return self.truncate("\n".join(lines[-count:]))

    def count_file(self, path: str, metric: str = "lines") -> int:
        """Count lines, words, or bytes in one policy-checked regular file."""

        target = self.safe_path(path)
        data, _ = self._read_bytes_and_state(target)
        if metric == "bytes":
            return len(data)
        if metric == "lines":
            return data.count(b"\n")
        if metric == "words":
            return len(data.decode("utf-8", errors="replace").split())
        raise WorkspaceError(f"unsupported count metric: {metric}")

    def find_paths(
        self,
        path: str = ".",
        *,
        max_depth: int | None = None,
        kind: str | None = None,
        name: str | None = None,
        path_glob: str | None = None,
    ) -> str:
        """Return a bounded, non-following path listing below ``path``."""

        target = self.safe_path(path)
        if not target.exists():
            raise WorkspaceError(f"path does not exist: {path}")
        if kind not in {None, "file", "directory"}:
            raise WorkspaceError(f"unsupported path kind: {kind}")
        narrowing_filter = (
            NarrowingPathFilter.compile(path_glob) if path_glob is not None else None
        )

        depth_limit = self.settings.max_tree_depth
        if max_depth is not None:
            depth_limit = max(0, min(max_depth, self.settings.max_tree_depth))

        state = _ScanState()
        results: list[str] = []

        def matches(candidate: Path, candidate_kind: str) -> bool:
            if kind is not None and kind != candidate_kind:
                return False
            if name is not None and not fnmatch.fnmatchcase(candidate.name, name):
                return False
            if narrowing_filter is None:
                return True
            return narrowing_filter.matches(self.relative_path(candidate))

        target_kind = "directory" if target.is_dir() else "file"
        if matches(target, target_kind):
            results.append(self.relative_path(target))

        if target.is_dir():
            pending: list[tuple[Path, int]] = [(target, 0)]
            while pending and not state.exhausted:
                directory, depth = pending.pop()
                if depth >= depth_limit:
                    continue
                scan_remaining = self.settings.max_scan_entries - state.scanned
                if scan_remaining <= 0:
                    state.exhausted = True
                    break
                if len(results) >= self.settings.max_tree_entries:
                    state.dropped += 1
                    break
                entries = self._scan_directory(
                    directory,
                    state,
                    keep_limit=scan_remaining,
                    skip_ignored=True,
                    strict=directory == target,
                )
                directories: list[Path] = []
                for entry in entries:
                    if entry.is_symlink:
                        state.skipped += 1
                        continue
                    entry_kind = "directory" if entry.is_directory else "file"
                    if matches(entry.path, entry_kind):
                        results.append(entry.path.relative_to(self.root).as_posix())
                        if len(results) >= self.settings.max_tree_entries:
                            state.dropped += 1
                            break
                    if entry.is_directory:
                        directories.append(entry.path)
                else:
                    pending.extend((item, depth + 1) for item in reversed(directories))
                    continue
                break

        self._append_scan_diagnostics(results, state, "find")
        return self.truncate("\n".join(results) or "No paths found.")

    def grep_file(self, text: str, path: str, max_results: int = 500) -> str:
        if not text:
            raise WorkspaceError("search text is empty")
        target = self.safe_path(path)
        matches: list[str] = []
        total_bytes = 0

        for line_number, line in enumerate(
            self._read_text(target, errors="replace").splitlines(), start=1
        ):
            if text not in line:
                continue
            rendered = f"{line_number}: {line}"
            matches.append(rendered)
            total_bytes += len(rendered.encode("utf-8")) + 1
            if len(matches) >= max_results:
                matches.append("... results truncated ...")
                break
            if total_bytes >= self.settings.max_output_size:
                break

        return self.truncate("\n".join(matches) or "No matches found.")

    def search_text(
        self,
        text: str,
        path: str = ".",
        max_results: int = 200,
        *,
        ignore_case: bool = False,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        if not text:
            raise WorkspaceError("search text is empty")

        comparable_needle = text.casefold() if ignore_case else text

        def matches(line: str, _budget: _SearchBudget) -> bool:
            comparable = line.casefold() if ignore_case else line
            return comparable_needle in comparable

        return self._search(
            matches,
            path,
            max_results,
            cancellation_event=cancellation_event,
            line_numbers=True,
            path_glob=None,
        )

    def search_pattern(
        self,
        pattern: str,
        path: str = ".",
        max_results: int = 200,
        *,
        fixed_strings: bool = False,
        ignore_case: bool = False,
        line_numbers: bool = False,
        path_glob: str | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> str:
        """Search using a literal or the documented non-backtracking regex subset."""

        if not pattern:
            raise WorkspaceError("search pattern is empty")
        narrowing_filter = (
            NarrowingPathFilter.compile(path_glob) if path_glob is not None else None
        )
        if fixed_strings:
            comparable_needle = pattern.casefold() if ignore_case else pattern

            def matches(line: str, _budget: _SearchBudget) -> bool:
                comparable = line.casefold() if ignore_case else line
                return comparable_needle in comparable

        else:
            expression = SafeRegex(pattern, ignore_case=ignore_case)

            def matches(line: str, budget: _SearchBudget) -> bool:
                return expression.search(line, should_stop=budget.should_stop)

        return self._search(
            matches,
            path,
            max_results,
            cancellation_event=cancellation_event,
            line_numbers=line_numbers,
            path_glob=narrowing_filter,
        )

    def _search(
        self,
        matcher: Callable[[str, _SearchBudget], bool],
        path: str,
        max_results: int,
        *,
        cancellation_event: threading.Event | None,
        line_numbers: bool,
        path_glob: NarrowingPathFilter | None,
    ) -> str:

        if not self._search_capacity.acquire(blocking=False):
            raise WorkspaceError("maximum concurrent search limit has been reached")
        try:
            return self._search_text_acquired(
                matcher,
                path,
                max_results,
                cancellation_event=cancellation_event,
                line_numbers=line_numbers,
                path_glob=path_glob,
            )
        finally:
            self._search_capacity.release()

    def _search_text_acquired(
        self,
        matcher: Callable[[str, _SearchBudget], bool],
        path: str,
        max_results: int,
        *,
        cancellation_event: threading.Event | None,
        line_numbers: bool,
        path_glob: NarrowingPathFilter | None,
    ) -> str:
        target = self.safe_path(path)
        if not target.exists():
            raise WorkspaceError(f"path does not exist: {path}")
        if not target.is_file() and not target.is_dir():
            raise WorkspaceError(f"path is not a regular file or directory: {path}")

        result_limit = max(1, min(max_results, 500))
        results: list[str] = []
        state = _ScanState()
        budget = _SearchBudget(
            max_bytes=self.settings.max_search_bytes,
            deadline=time.monotonic() + self.settings.search_timeout_seconds,
            cancellation_event=cancellation_event,
        )

        for file_path in self._iter_files(target, state, budget.should_stop):
            if budget.should_stop():
                break
            if path_glob is not None and not path_glob.matches(
                self.relative_path(file_path)
            ):
                continue
            try:
                return_limit = self._stream_search_file(
                    file_path,
                    matcher,
                    results,
                    result_limit,
                    budget,
                    line_numbers=line_numbers,
                )
            except WorkspaceError:
                state.skipped += 1
                continue
            if return_limit:
                results.append("... search return limit reached ...")
                return self.truncate("\n".join(results))

        if budget.stop_reason == "bytes":
            results.append(
                f"... search byte budget exhausted after {budget.bytes_read} bytes ..."
            )
        elif budget.stop_reason == "timeout":
            results.append("... search time budget exhausted ...")
        elif budget.stop_reason == "cancelled":
            results.append("... search cancelled ...")
        if state.exhausted:
            results.append("... search scan budget exhausted ...")
        if state.skipped:
            results.append(
                f"... search skipped {state.skipped} inaccessible entries ..."
            )
        return self.truncate("\n".join(results) or "No matches found.")

    def _stream_search_file(
        self,
        file_path: Path,
        matcher: Callable[[str, _SearchBudget], bool],
        results: list[str],
        result_limit: int,
        budget: _SearchBudget,
        *,
        line_numbers: bool,
    ) -> bool:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        pending = ""
        line_number = 1
        relative = self.relative_path(file_path)
        with self._open_regular_binary(file_path) as handle:
            while not budget.should_stop():
                remaining = budget.max_bytes - budget.bytes_read
                if remaining <= 0:
                    budget.stop_reason = "bytes"
                    break
                chunk = handle.read(min(_SEARCH_CHUNK_BYTES, remaining))
                if not chunk:
                    pending += decoder.decode(b"", final=True)
                    rendered_line = pending.rstrip("\r")
                    if pending and matcher(rendered_line, budget):
                        results.append(
                            self._render_search_match(
                                relative, line_number, pending.rstrip(), line_numbers
                            )
                        )
                    return len(results) >= result_limit
                budget.bytes_read += len(chunk)
                pending += decoder.decode(chunk, final=False)
                complete = pending.split("\n")
                pending = complete.pop()
                for line in complete:
                    rendered_line = line.rstrip("\r")
                    if matcher(rendered_line, budget):
                        results.append(
                            self._render_search_match(
                                relative,
                                line_number,
                                rendered_line.rstrip(),
                                line_numbers,
                            )
                        )
                        if len(results) >= result_limit:
                            return True
                        if len("\n".join(results).encode("utf-8")) >= (
                            self.settings.max_output_size
                        ):
                            return True
                    line_number += 1
                if budget.bytes_read >= budget.max_bytes:
                    budget.stop_reason = "bytes"
                    break
        return False

    @staticmethod
    def _render_search_match(
        relative: str, line_number: int, line: str, line_numbers: bool
    ) -> str:
        if line_numbers:
            return f"{relative}:{line_number}: {line}"
        return f"{relative}: {line}"

    def write_file(
        self,
        path: str,
        content: str,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> str:
        self._require_writable()
        self._check_content_size(content)
        target = self.safe_path(path)
        with self._lock_for_path(target):
            expected: _FileState | None = None
            if os.path.lexists(target):
                if not self._is_regular_path(target):
                    raise WorkspaceError("target is not a regular file")
                if not overwrite:
                    raise WorkspaceError(
                        "file already exists; use replace_text or set overwrite=true "
                        "intentionally"
                    )
                _, expected = self._read_bytes_and_state(target)
                self._require_expected_sha256(expected_sha256, expected)
            elif expected_sha256 is not None:
                raise WorkspaceError(
                    "conflict: expected_sha256 was supplied but the file does not exist"
                )
            self._atomic_write(target, content, expected=expected)
        return (
            f"Written successfully: {self.relative_path(target)}\n"
            f"Characters: {len(content)}\n"
            f"Bytes: {len(content.encode('utf-8'))}"
        )

    def replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str | None = None,
    ) -> str:
        self._require_writable()
        if not old_text:
            raise WorkspaceError("old_text cannot be empty")
        target = self.safe_path(path)
        with self._lock_for_path(target):
            data, state = self._read_bytes_and_state(target)
            self._require_expected_sha256(expected_sha256, state)
            try:
                content = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise WorkspaceError(
                    f"file is not valid UTF-8: {self.relative_path(target)}"
                ) from exc
            occurrence_count = content.count(old_text)
            if occurrence_count == 0:
                raise WorkspaceError(
                    "old_text was not found; read the file again before editing"
                )
            if occurrence_count > 1:
                raise WorkspaceError(
                    f"old_text occurs {occurrence_count} times; "
                    "replacement is ambiguous"
                )

            updated = content.replace(old_text, new_text, 1)
            self._check_content_size(updated)
            self._atomic_write(target, updated, expected=state)
        return f"Updated successfully: {self.relative_path(target)}\nReplacements: 1"

    def append_file(
        self, path: str, content: str, expected_sha256: str | None = None
    ) -> str:
        self._require_writable()
        target = self.safe_path(path)
        with self._lock_for_path(target):
            state: _FileState | None = None
            old_content = ""
            if os.path.lexists(target):
                data, state = self._read_bytes_and_state(target)
                self._require_expected_sha256(expected_sha256, state)
                try:
                    old_content = data.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise WorkspaceError(
                        f"file is not valid UTF-8: {self.relative_path(target)}"
                    ) from exc
            elif expected_sha256 is not None:
                raise WorkspaceError(
                    "conflict: expected_sha256 was supplied but the file does not exist"
                )
            updated = old_content + content
            self._check_content_size(updated)
            self._atomic_write(target, updated, expected=state)
        return f"Appended successfully: {self.relative_path(target)}"

    def _iter_files(
        self,
        target: Path,
        state: _ScanState,
        should_stop: object | None = None,
    ) -> Iterator[Path]:
        if target.is_file():
            state.scanned = 1
            yield target
            return

        pending = [target]
        stop = should_stop if callable(should_stop) else lambda: False
        while pending and not state.exhausted and not stop():
            current = pending.pop()
            remaining = self.settings.max_scan_entries - state.scanned
            if remaining <= 0:
                state.exhausted = True
                break
            entries = self._scan_directory(
                current,
                state,
                keep_limit=remaining,
                skip_ignored=True,
                strict=current == target,
                should_stop=stop,
            )
            directories: list[Path] = []
            for entry in entries:
                if entry.is_directory:
                    directories.append(entry.path)
                elif entry.is_symlink:
                    if entry.link_state is None and not entry.link_is_directory:
                        yield entry.path
                else:
                    yield entry.path
            pending.extend(reversed(directories))

    def _scan_directory(
        self,
        directory: Path,
        state: _ScanState,
        *,
        keep_limit: int,
        skip_ignored: bool,
        strict: bool,
        should_stop: object | None = None,
    ) -> list[_DirectoryEntry]:
        kept: list[tuple[tuple[bool, str, str], _DirectoryEntry]] = []
        try:
            with os.scandir(directory) as iterator:
                stop = should_stop if callable(should_stop) else lambda: False
                while state.scanned < self.settings.max_scan_entries and not stop():
                    try:
                        raw_entry = next(iterator)
                    except StopIteration:
                        break
                    state.scanned += 1
                    try:
                        entry = self._classify_entry(raw_entry)
                    except (OSError, RuntimeError, WorkspaceError):
                        state.skipped += 1
                        continue
                    if entry is None:
                        continue
                    if (
                        skip_ignored
                        and entry.is_directory
                        and entry.name in self.settings.ignored_dirs
                    ):
                        continue

                    candidate = (entry.sort_key, entry)
                    if len(kept) < keep_limit:
                        bisect.insort(kept, candidate)
                    elif keep_limit > 0 and candidate[0] < kept[-1][0]:
                        bisect.insort(kept, candidate)
                        kept.pop()
                        state.dropped += 1
                    else:
                        state.dropped += 1
                if state.scanned >= self.settings.max_scan_entries:
                    state.exhausted = True
        except OSError as exc:
            if strict:
                raise WorkspaceError(
                    f"cannot scan directory {self.relative_path(directory)}: {exc}"
                ) from exc
            state.skipped += 1
        return [entry for _, entry in kept]

    def _classify_entry(self, raw_entry: os.DirEntry[str]) -> _DirectoryEntry | None:
        path = Path(raw_entry.path)
        lexical_relative = self._relative_inside(path, raw_entry.name)
        if self.policy.is_blocked(lexical_relative.as_posix()):
            return None

        is_symlink = raw_entry.is_symlink()
        is_directory = raw_entry.is_dir(follow_symlinks=False)
        if not is_symlink:
            return _DirectoryEntry(raw_entry.name, path, is_directory, False)

        try:
            destination = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return _DirectoryEntry(
                raw_entry.name,
                path,
                False,
                True,
                link_state="broken",
            )
        try:
            destination_relative = destination.relative_to(self.root)
        except ValueError:
            return _DirectoryEntry(
                raw_entry.name,
                path,
                False,
                True,
                link_state="external",
            )
        if self.policy.is_blocked(destination_relative.as_posix()):
            return None
        return _DirectoryEntry(
            raw_entry.name,
            path,
            False,
            True,
            link_is_directory=destination.is_dir(),
        )

    @staticmethod
    def _render_list_entry(entry: _DirectoryEntry) -> str:
        if entry.link_state == "broken":
            return f"{entry.name} -> [BROKEN SYMLINK]"
        if entry.link_state == "external":
            return f"{entry.name} -> [BLOCKED EXTERNAL SYMLINK]"
        marker = "/" if entry.is_directory else ""
        return entry.name + marker

    @staticmethod
    def _append_scan_diagnostics(
        rendered: list[str], state: _ScanState, operation: str
    ) -> None:
        if state.dropped:
            rendered.append(f"... {operation} return limit reached ...")
        if state.exhausted:
            rendered.append(f"... {operation} scan budget exhausted ...")
        if state.skipped:
            rendered.append(
                f"... {operation} skipped {state.skipped} inaccessible entries ..."
            )

    def _existing_directory(self, value: str) -> Path:
        target = self.safe_path(value)
        if not target.exists():
            raise WorkspaceError(f"path does not exist: {value}")
        if not target.is_dir():
            raise WorkspaceError(f"not a directory: {value}")
        return target

    def _relative_inside(self, path: Path, original: str) -> Path:
        try:
            return path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"path escapes workspace: {original}") from exc

    def _require_allowed(self, relative: Path, original: str) -> None:
        pattern = self.policy.blocking_pattern(relative.as_posix())
        if pattern is not None:
            raise WorkspaceError(
                f"path is blocked by workspace policy ({pattern}): {original}"
            )

    def _read_text(self, path: Path, *, errors: str) -> str:
        data, _ = self._read_bytes_and_state(path)
        try:
            return data.decode("utf-8", errors=errors)
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                f"file is not valid UTF-8: {self.relative_path(path)}"
            ) from exc

    def _read_bytes_and_state(self, path: Path) -> tuple[bytes, _FileState]:
        descriptor = self._open_regular_descriptor(path)
        try:
            before = os.fstat(descriptor)
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(self.settings.max_file_size + 1)
                after = os.fstat(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(data) > self.settings.max_file_size:
            raise WorkspaceError(
                f"file is too large: more than {self.settings.max_file_size} bytes"
            )
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_identity != after_identity or after.st_size != len(data):
            raise WorkspaceError("conflict: file changed while it was being read")
        return data, _FileState(
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            mtime_ns=after.st_mtime_ns,
            device=after.st_dev,
            inode=after.st_ino,
            mode=stat.S_IMODE(after.st_mode),
        )

    @contextmanager
    def _open_regular_binary(self, path: Path) -> Iterator[BinaryIO]:
        descriptor = self._open_regular_descriptor(path)
        try:
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                yield handle
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _open_regular_descriptor(self, path: Path) -> int:
        relative = self._relative_inside(path, str(path))
        self._require_allowed(relative, str(path))
        try:
            preliminary = path.lstat()
        except FileNotFoundError as exc:
            raise WorkspaceError(
                f"file does not exist: {self.relative_path(path)}"
            ) from exc
        except OSError as exc:
            raise WorkspaceError(f"cannot inspect file safely: {path}: {exc}") from exc
        if not stat.S_ISREG(preliminary.st_mode):
            raise WorkspaceError(f"not a regular file: {self.relative_path(path)}")

        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI/users
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOINHERIT", 0)
                descriptor = os.open(path, flags)
            else:
                descriptor = self._open_relative_posix(relative)
        except OSError as exc:
            raise WorkspaceError(f"cannot open file safely: {path}: {exc}") from exc

        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise WorkspaceError(
                    f"not a regular file after open: {self.relative_path(path)}"
                )
            if opened.st_size > self.settings.max_file_size:
                raise WorkspaceError(
                    f"file is too large: more than {self.settings.max_file_size} bytes"
                )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _open_relative_posix(self, relative: Path) -> int:
        if not relative.parts:
            raise WorkspaceError("workspace root is a directory, not a regular file")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)

        directory_descriptors: list[int] = []
        try:
            current = os.open(self.root, directory_flags)
            directory_descriptors.append(current)
            for component in relative.parts[:-1]:
                current = os.open(
                    component,
                    directory_flags,
                    dir_fd=current,
                )
                directory_descriptors.append(current)
            return os.open(relative.parts[-1], file_flags, dir_fd=current)
        finally:
            for descriptor in reversed(directory_descriptors):
                os.close(descriptor)

    @staticmethod
    def _is_regular_path(path: Path) -> bool:
        try:
            return stat.S_ISREG(path.lstat().st_mode)
        except OSError:
            return False

    def _check_content_size(self, content: str) -> None:
        size = len(content.encode("utf-8"))
        if size > self.settings.max_file_size:
            raise WorkspaceError(
                f"content is too large: {size} bytes "
                f"(limit: {self.settings.max_file_size})"
            )

    def _require_writable(self) -> None:
        if not self.settings.allow_writes:
            raise WorkspaceError("server is running in read-only mode")

    def _atomic_write(
        self, path: Path, content: str, *, expected: _FileState | None
    ) -> None:
        parent = path.parent
        parent_relative = self._relative_inside(parent, str(parent))
        self._require_allowed(parent_relative, str(parent))

        try:
            parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = parent.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceError(
                f"cannot prepare target directory: {parent}: {exc}"
            ) from exc
        self._relative_inside(resolved_parent, str(parent))

        previous_mode = expected.mode if expected is not None else None

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".sandboxed_workspace_mcp_", dir=resolved_parent
        )
        temporary_path = Path(temporary_name)
        replaced = False
        descriptor_open = True
        try:
            if previous_mode is not None:
                os.fchmod(descriptor, previous_mode)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                descriptor_open = False
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if expected is None:
                if os.path.lexists(path):
                    raise WorkspaceError(
                        "conflict: target appeared before the new file was committed"
                    )
                try:
                    os.link(temporary_path, path, follow_symlinks=False)
                except FileExistsError as exc:
                    raise WorkspaceError(
                        "conflict: target appeared before the new file was committed"
                    ) from exc
                replaced = True
                temporary_path.unlink(missing_ok=True)
            else:
                self._verify_unchanged(path, expected)
                os.replace(temporary_path, path)
                replaced = True
            self._sync_directory(resolved_parent)
        except WorkspaceError:
            raise
        except OSError as exc:
            raise WorkspaceError(f"atomic write failed for {path}: {exc}") from exc
        finally:
            if descriptor_open:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not replaced:
                temporary_path.unlink(missing_ok=True)

    def _verify_unchanged(self, path: Path, expected: _FileState) -> None:
        try:
            if path.resolve(strict=True) != path:
                raise WorkspaceError("conflict: target path changed before commit")
        except OSError as exc:
            raise WorkspaceError("conflict: target disappeared before commit") from exc
        _, current = self._read_bytes_and_state(path)
        if current != expected:
            raise WorkspaceError(
                "conflict: file changed; call read_file_versioned and retry"
            )

    @contextmanager
    def _lock_for_path(self, path: Path) -> Iterator[None]:
        key = os.path.normcase(str(path))
        with self._path_locks_guard:
            lock = self._path_locks.setdefault(key, threading.Lock())
        with lock:
            yield

    @staticmethod
    def _require_expected_sha256(supplied: str | None, current: _FileState) -> None:
        if supplied is None:
            raise WorkspaceError(
                "conflict: expected_sha256 is required; call read_file_versioned first"
            )
        normalized = supplied.casefold()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise WorkspaceError("expected_sha256 must be a 64-character hex digest")
        if normalized != current.sha256:
            raise WorkspaceError(
                "conflict: file changed; call read_file_versioned and retry"
            )

    @staticmethod
    def _sync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)
