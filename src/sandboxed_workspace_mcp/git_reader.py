"""Read-only Git operations with bounded output and no user-controlled arguments."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections import defaultdict
from typing import BinaryIO

from .access_policy import AccessPolicy
from .bounded_output import BoundedText, truncate_utf8_result
from .config import Settings
from .workspace import Workspace, WorkspaceError, truncate_utf8


class GitError(RuntimeError):
    """Raised when a read-only Git operation cannot be completed safely."""


class _DiffOmissions:
    """Keep bounded, non-content diagnostics for files omitted from a diff."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self.examples: dict[str, list[str]] = defaultdict(list)

    def add(self, reason: str, path: str) -> None:
        self.counts[reason] += 1
        if len(self.examples[reason]) < 3:
            self.examples[reason].append(path)

    def render(self) -> str:
        lines = ["Omitted files:"]
        for reason in sorted(self.counts):
            examples = ", ".join(self.examples[reason])
            suffix = f" (examples: {examples})" if examples else ""
            lines.append(f"- {reason}: {self.counts[reason]}{suffix}")
        return "\n".join(lines) + "\n"

    def summary(self) -> str:
        return ", ".join(
            f"{reason}={self.counts[reason]}" for reason in sorted(self.counts)
        )


class _BoundedDiffOutput:
    """Append UTF-8 text without allowing aggregate output to grow unbounded."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def append(self, text: str) -> bool:
        if self.truncated or not text:
            return not self.truncated
        encoded = text.encode("utf-8")
        remaining = self.limit - self.size
        if len(encoded) <= remaining:
            self.parts.append(text)
            self.size += len(encoded)
            return True
        if remaining > 0:
            self.parts.append(encoded[:remaining].decode("utf-8", errors="ignore"))
            self.size = self.limit
        self.truncated = True
        return False

    def render_result(self, omissions: _DiffOmissions) -> BoundedText:
        text = "".join(self.parts)
        if not self.truncated:
            return BoundedText(text=text, truncated=False)

        marker = "\n... workspace_diff output truncated"
        if omissions.counts:
            marker += f"; omitted {omissions.summary()}"
        marker += " ...\n"
        marker_size = len(marker.encode("utf-8"))
        if marker_size >= self.limit:
            rendered = marker.encode("utf-8")[: self.limit].decode(
                "utf-8", errors="ignore"
            )
        else:
            rendered = truncate_utf8(text, self.limit - marker_size) + marker
        return BoundedText(text=rendered, truncated=True)

    def render(self, omissions: _DiffOmissions) -> str:
        return self.render_result(omissions).text


class GitReader:
    """Execute a closed set of read-only Git queries inside a workspace."""

    def __init__(self, settings: Settings, executable: str | None = None) -> None:
        self.settings = settings
        self.root = settings.root
        self.executable = executable or shutil.which("git")
        self.policy = AccessPolicy(settings.blocked_patterns)
        self.workspace = Workspace(settings)

    def status(self, style: str = "default") -> str:
        if style not in {"default", "short", "porcelain"}:
            raise GitError("git status style must be default, short, or porcelain")
        args = ["status"]
        if style == "default":
            args.extend(["--short", "--branch"])
        elif style == "short":
            args.append("--short")
        else:
            args.append("--porcelain=v1")
        args.extend(
            [
                "--untracked-files=normal",
                "--ignore-submodules=all",
                "--",
                ".",
                *self.policy.git_exclude_pathspecs(),
            ]
        )
        return self._run(args)

    def diff_result(
        self, *, staged: bool = False, path: str | None = None
    ) -> BoundedText:
        args = [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--ignore-submodules=all",
        ]
        if staged:
            args.append("--cached")
        args.extend(["--", *self._pathspecs(path)])
        return self._run_result(args)

    def diff(self, *, staged: bool = False, path: str | None = None) -> str:
        return self.diff_result(staged=staged, path=path).text

    def workspace_diff_result(self, *, path: str | None = None) -> BoundedText:
        """Show the final safe workspace state relative to the Git baseline."""

        pathspecs = self._pathspecs(path)
        self._ensure_work_tree()
        omissions = _DiffOmissions()
        output = _BoundedDiffOutput(self.settings.max_output_size)
        changed = False

        if self._has_commit():
            tracked = self._run(
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--ignore-submodules=all",
                    "HEAD",
                    "--",
                    *pathspecs,
                ]
            )
            if tracked != "(no output)":
                output.append("Tracked changes (HEAD vs working tree):\n")
                output.append(tracked)
                if not tracked.endswith("\n"):
                    output.append("\n")
                changed = True
        else:
            tracked_paths = self._limit_paths(
                self._list_paths(["ls-files", "-z", "--", *pathspecs]),
                omissions,
                self.settings.max_scan_entries,
            )
            if tracked_paths:
                output.append("Tracked changes (relative to empty HEAD):\n")
                changed = (
                    self._append_new_files(output, tracked_paths, omissions) or changed
                )

        untracked_paths = self._limit_paths(
            self._list_paths(
                [
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "-z",
                    "--",
                    *pathspecs,
                ]
            ),
            omissions,
            self.settings.max_scan_entries,
        )
        if untracked_paths:
            output.append("Untracked changes:\n")
            for index, candidate in enumerate(untracked_paths):
                if output.truncated:
                    omissions.add("output limit", self._display_path(candidate))
                    continue
                if self._append_new_file(output, candidate, omissions):
                    changed = True
                if output.truncated:
                    for remaining in untracked_paths[index + 1 :]:
                        omissions.add("output limit", self._display_path(remaining))
                    break

        if omissions.counts:
            output.append("\n")
            output.append(omissions.render())
        if not changed and not omissions.counts:
            return BoundedText(text="(no output)", truncated=False)
        rendered = output.render_result(omissions)
        if rendered.text:
            return rendered
        return BoundedText(text="(no output)", truncated=False)

    def workspace_diff(self, *, path: str | None = None) -> str:
        return self.workspace_diff_result(path=path).text

    def log_result(self, count: int = 10, *, oneline: bool = False) -> BoundedText:
        if type(count) is not int or not 1 <= count <= 50:
            raise GitError("git log count must be an integer between 1 and 50")
        # The stable one-line format remains the only output format. ``oneline`` is
        # an explicit compatibility selector, not a switch to a second renderer.
        if type(oneline) is not bool:
            raise GitError("git log oneline must be a boolean")
        return self._run_result(
            [
                "log",
                f"-{count}",
                "--oneline",
                "--decorate",
                "--no-show-signature",
            ]
        )

    def log(self, count: int = 10, *, oneline: bool = False) -> str:
        return self.log_result(count, oneline=oneline).text

    def show_result(self, commit: str, *, path: str | None = None) -> BoundedText:
        if (
            not isinstance(commit, str)
            or re.fullmatch(r"(?:HEAD|[0-9a-fA-F]{7,40})", commit) is None
        ):
            raise GitError(
                "git show commit must be HEAD or a 7-40 character hexadecimal ID"
            )
        commit_object = f"{commit}^{{commit}}"
        return self._run_result(
            [
                "show",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-show-signature",
                commit_object,
                "--",
                *self._pathspecs(path),
            ]
        )

    def show(self, commit: str, *, path: str | None = None) -> str:
        return self.show_result(commit, path=path).text

    def branches(self, *, show_current: bool = False) -> str:
        if type(show_current) is not bool:
            raise GitError("git branch show_current must be a boolean")
        if show_current:
            return self._run(["branch", "--show-current", "--no-color"])
        return self._run(["branch", "--list", "--no-color"])

    def rev_parse(self, query: str) -> str:
        allowed_queries = {
            "--is-inside-work-tree",
            "--show-prefix",
            "--show-toplevel",
            "HEAD",
        }
        if query not in allowed_queries:
            raise GitError(f"git rev-parse query is not allowed: {query}")
        return self._run(["rev-parse", query])

    def ls_files(self) -> str:
        return self._run(["ls-files", "--", ".", *self.policy.git_exclude_pathspecs()])

    def _pathspecs(self, path: str | None) -> list[str]:
        if path is None:
            return [".", *self.policy.git_exclude_pathspecs()]
        if not isinstance(path, str) or not path:
            raise GitError("git path must be a non-empty string")
        target = self.workspace.safe_path(path)
        relative = self.workspace.relative_path(target)
        return [f":(literal){relative}", *self.policy.git_exclude_pathspecs()]

    def _ensure_work_tree(self) -> None:
        self._run(["rev-parse", "--is-inside-work-tree"])

    def _has_commit(self) -> bool:
        try:
            self._run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
        except GitError as exc:
            if "exit code 1" in str(exc):
                return False
            raise
        return True

    def _list_paths(self, args: list[str]) -> list[str]:
        rendered = self._run(args)
        if rendered == "(no output)":
            return []
        return sorted({path for path in rendered.split("\0") if path})

    def _limit_paths(
        self,
        paths: list[str],
        omissions: _DiffOmissions,
        limit: int,
    ) -> list[str]:
        if len(paths) <= limit:
            return paths
        for candidate in paths[limit:]:
            omissions.add("scan limit", self._display_path(candidate))
        return paths[:limit]

    def _append_new_files(
        self,
        output: _BoundedDiffOutput,
        paths: list[str],
        omissions: _DiffOmissions,
    ) -> bool:
        changed = False
        for index, candidate in enumerate(paths):
            if output.truncated:
                omissions.add("output limit", self._display_path(candidate))
                continue
            if self._append_new_file(output, candidate, omissions):
                changed = True
            if output.truncated:
                for remaining in paths[index + 1 :]:
                    omissions.add("output limit", self._display_path(remaining))
                break
        return changed

    def _append_new_file(
        self,
        output: _BoundedDiffOutput,
        candidate: str,
        omissions: _DiffOmissions,
    ) -> bool:
        try:
            relative = self.workspace.relative_path(
                self.workspace.safe_regular_file_path(candidate)
            )
            data = self.workspace.read_file_bytes(candidate)
        except WorkspaceError as exc:
            omissions.add(
                self._workspace_omission_reason(str(exc)),
                self._display_path(candidate),
            )
            return False

        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            omissions.add("binary", self._display_path(relative))
            return False
        if "\x00" in text:
            omissions.add("binary", self._display_path(relative))
            return False

        for chunk in self._new_file_diff(relative, text):
            if not output.append(chunk):
                omissions.add("output limit", self._display_path(relative))
                return False
        return True

    @staticmethod
    def _new_file_diff(relative: str, text: str):
        quoted = GitReader._quote_diff_path(relative)
        yield f"diff --git a/{quoted} b/{quoted}\n"
        yield "new file mode 100644\n"
        yield "--- /dev/null\n"
        yield f"+++ b/{quoted}\n"
        line_count = text.count("\n")
        if text and not text.endswith("\n"):
            line_count += 1
        yield f"@@ -0,0 +1,{line_count} @@\n"
        start = 0
        while start < len(text):
            end = text.find("\n", start)
            if end < 0:
                yield "+" + text[start:] + "\n"
                yield "\\ No newline at end of file\n"
                break
            yield "+" + text[start : end + 1]
            start = end + 1

    @staticmethod
    def _quote_diff_path(path: str) -> str:
        escaped: list[str] = []
        needs_quotes = not path or path[0] in {"-", ":"}
        for character in path:
            codepoint = ord(character)
            if character == "\\":
                escaped.append("\\\\")
                needs_quotes = True
            elif character == '"':
                escaped.append('\\"')
                needs_quotes = True
            elif character == "\n":
                escaped.append("\\n")
                needs_quotes = True
            elif character == "\r":
                escaped.append("\\r")
                needs_quotes = True
            elif character == "\t":
                escaped.append("\\t")
                needs_quotes = True
            elif codepoint < 0x20 or codepoint == 0x7F:
                escaped.append(f"\\x{codepoint:02x}")
                needs_quotes = True
            else:
                escaped.append(character)
        rendered = "".join(escaped)
        return f'"{rendered}"' if needs_quotes else rendered

    @staticmethod
    def _display_path(path: str) -> str:
        return GitReader._quote_diff_path(truncate_utf8(path, 200))

    @staticmethod
    def _workspace_omission_reason(message: str) -> str:
        lowered = message.lower()
        if "blocked" in lowered:
            return "blocked"
        if "symbolic link" in lowered:
            return "symlink"
        if "not a regular file" in lowered:
            return "special"
        if "too large" in lowered:
            return "oversized"
        if "changed while" in lowered:
            return "read conflict"
        if "does not exist" in lowered:
            return "unavailable"
        return "unsafe"

    def _run_result(self, args: list[str]) -> BoundedText:
        if not self.executable:
            raise GitError("git executable was not found")

        command = [
            self.executable,
            "--no-pager",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            f"core.excludesFile={os.devnull}",
            "-c",
            "diff.external=",
            "-c",
            "diff.trustExitCode=false",
            *args,
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as exc:
            raise GitError(f"failed to start git executable: {exc}") from exc

        if process.stdout is None or process.stderr is None:  # pragma: no cover
            process.kill()
            raise GitError("failed to capture git output")

        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()
        output_lock = threading.Lock()

        def consume_output(stream: BinaryIO, destination: bytearray) -> None:
            try:
                while chunk := stream.read(64 * 1024):
                    with output_lock:
                        remaining = (
                            self.settings.max_output_size
                            + 1
                            - len(stdout)
                            - len(stderr)
                        )
                        if remaining > 0:
                            destination.extend(chunk[:remaining])
                        if len(chunk) > remaining or (
                            len(stdout) + len(stderr) > self.settings.max_output_size
                        ):
                            overflow.set()
                    if overflow.is_set():
                        try:
                            process.terminate()
                        except OSError:
                            pass
                        return
            except OSError:
                return

        readers = [
            threading.Thread(
                target=consume_output,
                args=(process.stdout, stdout),
                daemon=True,
            ),
            threading.Thread(
                target=consume_output,
                args=(process.stderr, stderr),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            process.wait(timeout=self.settings.git_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        finally:
            for reader in readers:
                reader.join(timeout=1.0)
            process.stdout.close()
            process.stderr.close()

        diagnostic = self._diagnostic(stdout, stderr)
        if overflow.is_set():
            raise GitError(
                self._bounded_error(
                    f"git output exceeded {self.settings.max_output_size} bytes",
                    diagnostic,
                )
            )
        if timed_out:
            raise GitError(
                self._bounded_error(
                    "git command timed out after "
                    f"{self.settings.git_timeout:g} seconds",
                    diagnostic,
                )
            )
        if process.returncode:
            raise GitError(
                self._bounded_error(
                    f"git command failed with exit code {process.returncode}",
                    diagnostic,
                )
            )

        text = bytes(stdout).decode("utf-8", errors="replace")
        if stderr:
            text += ("\n" if text else "") + bytes(stderr).decode(
                "utf-8", errors="replace"
            )
        return truncate_utf8_result(
            text or "(no output)", self.settings.max_output_size
        )

    def _run(self, args: list[str]) -> str:
        return self._run_result(args).text

    def _diagnostic(self, stdout: bytearray, stderr: bytearray) -> str:
        sections: list[str] = []
        if stderr:
            sections.append(
                "stderr: " + bytes(stderr).decode("utf-8", errors="replace").strip()
            )
        if stdout:
            sections.append(
                "stdout: " + bytes(stdout).decode("utf-8", errors="replace").strip()
            )
        return truncate_utf8("\n".join(sections), self.settings.max_output_size)

    def _bounded_error(self, message: str, diagnostic: str) -> str:
        if diagnostic:
            message = f"{message}\n{diagnostic}"
        return truncate_utf8(message, self.settings.max_output_size)

    def _environment(self) -> dict[str, str]:
        safe_tempdir = "/tmp" if os.name != "nt" else str(self.root)
        environment = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": os.devnull,
            "GIT_SEQUENCE_EDITOR": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "HOME": str(self.root),
            "LANG": "C",
            "LC_ALL": "C",
            "PAGER": "cat",
            "PATH": os.defpath,
            "TMPDIR": safe_tempdir,
            "TEMP": safe_tempdir,
            "TMP": safe_tempdir,
        }
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            environment["USERPROFILE"] = str(self.root)
        return environment
