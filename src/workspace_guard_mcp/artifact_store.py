"""Bounded disk-backed ephemeral storage for admitted execution artifacts."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .artifact import ArtifactRecord, is_safe_artifact_name
from .task_config import TaskLimits

ARTIFACT_URI_PREFIX = "workspaceguard://artifact/"
ARTIFACT_URI_TEMPLATE = ARTIFACT_URI_PREFIX + "{id}"
ARTIFACT_RESOURCE_MIME = "application/octet-stream"
DEFAULT_MAX_RETAINED_EXECUTIONS = 128
DEFAULT_MAX_STORE_BYTES = 512 * 1024 * 1024
DEFAULT_TTL_SECONDS = 60 * 60.0
_COPY_CHUNK_BYTES = 64 * 1024
_MAX_COLLISION_RETRIES = 16
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")


class ArtifactError(RuntimeError):
    """Base class for fail-closed artifact processing errors."""


class ArtifactLimitExceeded(ArtifactError):
    """Raised when per-execution artifact limits are exceeded."""


class ArtifactPolicyViolation(ArtifactError):
    """Raised when staging contains unsupported or unsafe artifact entries."""


class ArtifactCollectionError(ArtifactError):
    """Raised for server-side admission/storage failures."""


class ArtifactStoreMiss(LookupError):
    """Raised for invalid, missing, expired, evicted, or wrong-owner artifacts."""


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    record: ArtifactRecord
    path: Path


@dataclass(slots=True)
class _ExecutionArtifacts:
    artifacts: list[StoredArtifact]
    total_bytes: int
    created_at: float
    expires_at: float
    owner_scope: str | None


class EphemeralArtifactStore:
    """Thread-safe process-local store that evicts whole execution artifact sets."""

    def __init__(
        self,
        *,
        max_retained_executions: int = DEFAULT_MAX_RETAINED_EXECUTIONS,
        max_store_bytes: int = DEFAULT_MAX_STORE_BYTES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if type(max_retained_executions) is not int or max_retained_executions <= 0:
            raise ValueError("max_retained_executions must be a positive integer")
        if type(max_store_bytes) is not int or max_store_bytes <= 0:
            raise ValueError("max_store_bytes must be a positive integer")
        if not isinstance(ttl_seconds, int | float) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self.max_retained_executions = max_retained_executions
        self.max_store_bytes = max_store_bytes
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory or self._new_artifact_id
        self._temporary = tempfile.TemporaryDirectory(
            prefix="workspace-guard-mcp-artifact-store-"
        )
        self._root = Path(self._temporary.name).resolve()
        self._executions: OrderedDict[str, _ExecutionArtifacts] = OrderedDict()
        self._artifact_index: dict[str, tuple[str, StoredArtifact]] = {}
        self._total_bytes = 0
        self._lock = threading.RLock()

    @staticmethod
    def _new_artifact_id() -> str:
        return secrets.token_urlsafe(24)

    @staticmethod
    def valid_artifact_id(artifact_id: str) -> bool:
        return isinstance(artifact_id, str) and bool(
            _ARTIFACT_ID.fullmatch(artifact_id)
        )

    def collect(
        self,
        execution_id: str,
        staging_path: Path,
        limits: TaskLimits,
        *,
        owner_scope: str | None = None,
    ) -> list[ArtifactRecord]:
        """Validate staging and atomically publish immutable admitted copies."""

        candidates = self._inspect_staging(staging_path, limits)
        temp_objects: list[tuple[str, Path, str, int, str, float]] = []
        try:
            total = 0
            for name, expected in candidates:
                artifact_id = self._reserve_artifact_id()
                object_path = self._root / f"pending-{artifact_id}"
                size_bytes, digest = self._copy_candidate(
                    staging_path,
                    name,
                    expected,
                    object_path,
                    limits,
                    total,
                )
                total += size_bytes
                temp_objects.append(
                    (artifact_id, object_path, name, size_bytes, digest, time.time())
                )
        except (ArtifactLimitExceeded, ArtifactPolicyViolation):
            self._cleanup_paths(item[1] for item in temp_objects)
            raise
        except Exception as exc:
            self._cleanup_paths(item[1] for item in temp_objects)
            raise ArtifactCollectionError("artifact collection failed") from exc

        now = self._clock()
        with self._lock:
            self._evict_expired_locked(now)
            if execution_id in self._executions:
                self._cleanup_paths(item[1] for item in temp_objects)
                raise ArtifactCollectionError(
                    "artifacts already admitted for execution"
                )
            execution_bytes = sum(item[3] for item in temp_objects)
            if execution_bytes > self.max_store_bytes:
                self._cleanup_paths(item[1] for item in temp_objects)
                raise ArtifactCollectionError("artifact set exceeds store capacity")
            while (
                len(self._executions) >= self.max_retained_executions
                or self._total_bytes + execution_bytes > self.max_store_bytes
            ):
                self._evict_oldest_locked()
            stored: list[StoredArtifact] = []
            try:
                for (
                    artifact_id,
                    pending,
                    name,
                    size_bytes,
                    digest,
                    created_at,
                ) in temp_objects:
                    final_path = self._root / artifact_id
                    os.replace(pending, final_path)
                    record = ArtifactRecord(
                        artifact_id=artifact_id,
                        execution_id=execution_id,
                        name=name,
                        media_type=mimetypes.guess_type(name, strict=False)[0]
                        or ARTIFACT_RESOURCE_MIME,
                        size_bytes=size_bytes,
                        sha256=digest,
                        created_at=created_at,
                    )
                    stored.append(StoredArtifact(record=record, path=final_path))
            except Exception as exc:
                self._cleanup_paths(item.path for item in stored)
                self._cleanup_paths(item[1] for item in temp_objects)
                raise ArtifactCollectionError("artifact publish failed") from exc
            entry = _ExecutionArtifacts(
                artifacts=stored,
                total_bytes=execution_bytes,
                created_at=now,
                expires_at=now + self.ttl_seconds,
                owner_scope=owner_scope,
            )
            self._executions[execution_id] = entry
            self._total_bytes += execution_bytes
            for item in stored:
                self._artifact_index[item.record.artifact_id] = (execution_id, item)
            return [item.record for item in stored]

    def list_execution(
        self,
        execution_id: str,
        *,
        owner_scope: str | None = None,
    ) -> list[ArtifactRecord]:
        now = self._clock()
        with self._lock:
            self._evict_expired_locked(now)
            entry = self._executions.get(execution_id)
            if entry is None or entry.owner_scope != owner_scope:
                return []
            self._executions.move_to_end(execution_id)
            return [item.record for item in entry.artifacts]

    def read(
        self,
        artifact_id: str,
        *,
        owner_scope: str | None = None,
    ) -> bytes:
        if not self.valid_artifact_id(artifact_id):
            raise ArtifactStoreMiss("artifact not found")
        now = self._clock()
        with self._lock:
            self._evict_expired_locked(now)
            indexed = self._artifact_index.get(artifact_id)
            if indexed is None:
                raise ArtifactStoreMiss("artifact not found")
            execution_id, item = indexed
            entry = self._executions.get(execution_id)
            if entry is None or entry.owner_scope != owner_scope:
                raise ArtifactStoreMiss("artifact not found")
            self._executions.move_to_end(execution_id)
            path = item.path
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ArtifactStoreMiss("artifact not found") from exc

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def execution_count(self) -> int:
        with self._lock:
            return len(self._executions)

    def _reserve_artifact_id(self) -> str:
        with self._lock:
            for _ in range(_MAX_COLLISION_RETRIES):
                artifact_id = self._token_factory()
                if not self.valid_artifact_id(artifact_id):
                    raise ArtifactCollectionError(
                        "artifact ID generator returned an invalid token"
                    )
                if artifact_id not in self._artifact_index and not (
                    self._root / artifact_id
                ).exists():
                    return artifact_id
        raise ArtifactCollectionError("artifact ID collision retry limit exceeded")

    def _inspect_staging(
        self, staging_path: Path, limits: TaskLimits
    ) -> list[tuple[str, os.stat_result]]:
        try:
            root = staging_path.resolve(strict=True)
            iterator = os.scandir(root)
        except OSError as exc:
            raise ArtifactCollectionError("cannot inspect artifact staging") from exc
        candidates: list[tuple[str, os.stat_result]] = []
        total = 0
        with iterator:
            for entry in iterator:
                name = entry.name
                if not is_safe_artifact_name(name):
                    raise ArtifactPolicyViolation("unsafe artifact name")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ArtifactCollectionError(
                        "cannot stat artifact staging entry"
                    ) from exc
                if not stat.S_ISREG(metadata.st_mode):
                    raise ArtifactPolicyViolation(
                        "artifact must be a top-level regular file"
                    )
                if metadata.st_nlink != 1:
                    raise ArtifactPolicyViolation("artifact hard links are not allowed")
                if metadata.st_size > limits.max_artifact_bytes:
                    raise ArtifactLimitExceeded("artifact exceeds max_artifact_bytes")
                total += metadata.st_size
                if total > limits.max_total_artifact_bytes:
                    raise ArtifactLimitExceeded(
                        "artifacts exceed max_total_artifact_bytes"
                    )
                candidates.append((name, metadata))
                if len(candidates) > limits.max_artifacts_per_execution:
                    raise ArtifactLimitExceeded(
                        "artifacts exceed max_artifacts_per_execution"
                    )
        candidates.sort(key=lambda item: item[0])
        return candidates

    def _copy_candidate(
        self,
        staging_path: Path,
        name: str,
        expected: os.stat_result,
        destination: Path,
        limits: TaskLimits,
        existing_total: int,
    ) -> tuple[int, str]:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            directory_fd = os.open(staging_path, directory_flags)
        except OSError as exc:
            raise ArtifactCollectionError("cannot open artifact staging safely") from exc
        descriptor = -1
        output = -1
        size_bytes = 0
        digest = hashlib.sha256()
        try:
            descriptor = os.open(name, file_flags, dir_fd=directory_fd)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ArtifactPolicyViolation("artifact changed type while collecting")
            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                raise ArtifactPolicyViolation("artifact changed while collecting")
            output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            output_flags |= getattr(os, "O_CLOEXEC", 0)
            output = os.open(destination, output_flags, 0o600)
            while True:
                chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > limits.max_artifact_bytes:
                    raise ArtifactLimitExceeded("artifact exceeds max_artifact_bytes")
                if existing_total + size_bytes > limits.max_total_artifact_bytes:
                    raise ArtifactLimitExceeded(
                        "artifacts exceed max_total_artifact_bytes"
                    )
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(output, view)
                    view = view[written:]
            final = os.fstat(descriptor)
            if (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino):
                raise ArtifactPolicyViolation("artifact changed while collecting")
            return size_bytes, digest.hexdigest()
        except BaseException:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            if output >= 0:
                os.close(output)
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)

    def _evict_expired_locked(self, now: float) -> None:
        expired = [
            execution_id
            for execution_id, entry in self._executions.items()
            if now >= entry.expires_at
        ]
        for execution_id in expired:
            self._remove_execution_locked(execution_id)

    def _evict_oldest_locked(self) -> None:
        if not self._executions:
            return
        self._remove_execution_locked(next(iter(self._executions)))

    def _remove_execution_locked(self, execution_id: str) -> None:
        entry = self._executions.pop(execution_id)
        self._total_bytes -= entry.total_bytes
        for item in entry.artifacts:
            self._artifact_index.pop(item.record.artifact_id, None)
            try:
                item.path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _cleanup_paths(paths) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
