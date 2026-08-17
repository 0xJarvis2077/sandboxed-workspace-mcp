"""A bounded, recoverable recycle bin for one :class:`Workspace`."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, TypedDict, overload

from .access_policy import TRASH_DIRECTORY_NAME
from .workspace import RestoreTargetError, Workspace, WorkspaceError

_FORMAT_VERSION = 1
_METADATA_MAX_BYTES = 64 * 1024
_TRASH_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_DIAGNOSTICS = 20
_ITEM_FILES = frozenset({"metadata.json", "payload", "restore-intent.json"})

TRASH_DISABLED = "TRASH_DISABLED"
TRASH_ID_INVALID = "TRASH_ID_INVALID"
TRASH_ITEM_NOT_FOUND = "TRASH_ITEM_NOT_FOUND"
TRASH_ITEM_CORRUPT = "TRASH_ITEM_CORRUPT"
TRASH_STORAGE_CORRUPT = "TRASH_STORAGE_CORRUPT"
TRASH_STORAGE_PERMISSION_DENIED = "TRASH_STORAGE_PERMISSION_DENIED"
TRASH_STORAGE_IO_ERROR = "TRASH_STORAGE_IO_ERROR"
TRASH_VERSION_CONFLICT = "TRASH_VERSION_CONFLICT"
TRASH_DESTINATION_INVALID = "TRASH_DESTINATION_INVALID"
TRASH_DESTINATION_EXISTS = "TRASH_DESTINATION_EXISTS"
TRASH_QUOTA_EXCEEDED = "TRASH_QUOTA_EXCEEDED"
TRASH_OPERATION_CONFLICT = "TRASH_OPERATION_CONFLICT"


class TrashError(WorkspaceError):
    """A domain error with a stable public code and bounded details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)

    @property
    def public_error(self) -> dict[str, object]:
        error: dict[str, object] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            error["details"] = dict(self.details)
        return {"error": error}


@dataclass(frozen=True, slots=True)
class _Store:
    root: Path
    staging: Path
    items: Path
    purging: Path


class _RestoreIntent(TypedDict):
    version: int
    trash_id: str
    destination_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class _Record:
    metadata: dict[str, Any]
    item_dir: Path
    payload: Path


class TrashManager:
    """Own the fixed internal recycle-bin format and its transactions."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.settings = workspace.settings
        self._global_lock = threading.RLock()

    def trash_file(self, path: str, expected_sha256: str) -> dict[str, object]:
        """Move one version-checked regular file into the protected recycle bin."""

        self._require_enabled()
        self._reject_glob(path)
        with self._global_lock:
            if self._recover():
                raise TrashError(
                    TRASH_OPERATION_CONFLICT,
                    "trash contains invalid entries; repair them before adding "
                    "another file",
                )
            target = self.workspace.safe_regular_file_path(path)
            with self.workspace._lock_for_path(target):
                data, state = self.workspace._read_bytes_and_state(target)
                try:
                    self.workspace._require_expected_sha256(expected_sha256, state)
                except WorkspaceError as exc:
                    raise TrashError(
                        TRASH_VERSION_CONFLICT,
                        "conflict: file changed; list it again and retry",
                    ) from exc
                records, diagnostics = self._load_records()
                if diagnostics:
                    raise TrashError(
                        TRASH_OPERATION_CONFLICT,
                        "trash contains invalid entries; repair them before adding "
                        "another file",
                    )
                if len(records) >= self.settings.max_trash_items:
                    raise TrashError(
                        TRASH_QUOTA_EXCEEDED,
                        "trash item limit has been reached",
                        details={"max_items": self.settings.max_trash_items},
                    )
                used_bytes = sum(record.metadata["size"] for record in records)
                if used_bytes + len(data) > self.settings.max_trash_bytes:
                    raise TrashError(
                        TRASH_QUOTA_EXCEEDED,
                        "trash byte limit has been reached",
                        details={"max_bytes": self.settings.max_trash_bytes},
                    )

                store = self._ensure_store(create=True)
                trash_id = self._new_trash_id(store)
                staging_item = store.staging / trash_id
                try:
                    staging_item.mkdir(mode=0o700)
                except OSError as exc:
                    raise self._storage_error(
                        "cannot create trash staging item", exc
                    ) from exc
                metadata = {
                    "version": _FORMAT_VERSION,
                    "trash_id": trash_id,
                    "original_path": self.workspace.relative_path(target),
                    "sha256": state.sha256,
                    "size": state.size,
                    "mtime_ns": state.mtime_ns,
                    "mode": state.mode,
                    "trashed_at": time.time_ns(),
                }
                try:
                    self._write_json(staging_item / "metadata.json", metadata)
                    self.workspace._verify_unchanged(target, state)
                    payload = staging_item / "payload"
                    os.replace(target, payload)
                    self._chmod_file(payload, 0o600)
                    self.workspace._sync_directory(target.parent)
                    self.workspace._sync_directory(staging_item)
                    final_item = store.items / trash_id
                    os.replace(staging_item, final_item)
                    self.workspace._sync_directory(store.items)
                except WorkspaceError:
                    raise
                except OSError as exc:
                    raise TrashError(
                        TRASH_OPERATION_CONFLICT,
                        "trash transaction failed; the source may still be recoverable",
                        details={"trash_id": trash_id},
                    ) from exc
                return self._public_metadata(metadata)

    def list_trashed_files(self, offset: int = 0, limit: int = 50) -> dict[str, object]:
        """Return one bounded metadata-only page of recycle-bin entries."""

        self._require_enabled()
        if type(offset) is not int or offset < 0:
            raise TrashError(TRASH_OPERATION_CONFLICT, "offset must not be negative")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise TrashError(
                TRASH_OPERATION_CONFLICT, "limit must be between 1 and 200"
            )
        with self._global_lock:
            diagnostics = self._recover()
            records, load_diagnostics = self._load_records()
            diagnostics.extend(load_diagnostics)
            diagnostics = self._bounded_diagnostics(diagnostics)
            records.sort(
                key=lambda record: (
                    -record.metadata["trashed_at"],
                    record.metadata["trash_id"],
                )
            )
            total = len(records)
            page = [
                self._public_metadata(record.metadata)
                for record in records[offset : offset + limit]
            ]
            result: dict[str, object] = {
                "items": page,
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(page) < total,
            }
            if diagnostics:
                result["diagnostics"] = diagnostics
            self._fit_list_output(result)
            return result

    def restore_trashed_file(
        self,
        trash_id: str,
        expected_sha256: str,
        destination_path: str | None = None,
    ) -> dict[str, object]:
        """Restore one entry without overwriting any existing workspace path."""

        self._require_enabled()
        self._validate_trash_id(trash_id)
        with self._global_lock:
            self._recover()
            store = self._ensure_store(create=False)
            if store is None:
                raise self._item_not_found(trash_id)
            item_dir = self._locate_item(store, trash_id)
            record = self._read_record(item_dir, trash_id)
            if self._path_exists(item_dir / "restore-intent.json"):
                raise TrashError(
                    TRASH_OPERATION_CONFLICT,
                    "restore operation is still pending for this trash item",
                    details={"trash_id": trash_id},
                )
            data, _ = self._read_payload(record)
            self._verify_record_digest(record, data)
            self._validate_expected_sha256(expected_sha256, record.metadata["sha256"])

            target, restored_path = self._restore_target(
                record.metadata, destination_path
            )
            with self.workspace._lock_for_path(target):
                try:
                    target.lstat()
                except FileNotFoundError:
                    pass
                except PermissionError as exc:
                    raise TrashError(
                        TRASH_STORAGE_PERMISSION_DENIED,
                        "restore destination cannot be inspected",
                        details={"restored_path": restored_path},
                    ) from exc
                except OSError as exc:
                    raise TrashError(
                        TRASH_STORAGE_IO_ERROR,
                        "restore destination cannot be inspected",
                        details={"restored_path": restored_path},
                    ) from exc
                else:
                    raise TrashError(
                        TRASH_DESTINATION_EXISTS,
                        "restore destination already exists",
                        details={"restored_path": restored_path},
                    )

                intent = {
                    "version": 1,
                    "trash_id": trash_id,
                    "destination_path": restored_path,
                    "sha256": record.metadata["sha256"],
                }
                self._write_json(record.item_dir / "restore-intent.json", intent)
                target_created = False
                try:
                    self._chmod_file(record.payload, record.metadata["mode"])
                    os.link(record.payload, target, follow_symlinks=False)
                    target_created = True
                    os.utime(
                        target,
                        ns=(record.metadata["mtime_ns"], record.metadata["mtime_ns"]),
                        follow_symlinks=False,
                    )
                    self.workspace._sync_directory(target.parent)
                    record.payload.unlink()
                    record.item_dir.joinpath("metadata.json").unlink()
                    record.item_dir.joinpath("restore-intent.json").unlink()
                    record.item_dir.rmdir()
                    self.workspace._sync_directory(store.items)
                except FileExistsError as exc:
                    self._remove_intent_best_effort(record.item_dir)
                    raise TrashError(
                        TRASH_DESTINATION_EXISTS,
                        "restore destination already exists",
                        details={"restored_path": restored_path},
                    ) from exc
                except OSError as exc:
                    if not target_created:
                        self._remove_intent_best_effort(record.item_dir)
                    raise TrashError(
                        TRASH_OPERATION_CONFLICT,
                        "restore transaction failed; the payload was retained",
                        details={"trash_id": trash_id},
                    ) from exc
            return {
                "status": "restored",
                "restored": True,
                "trash_id": trash_id,
                "original_path": record.metadata["original_path"],
                "restored_path": restored_path,
                "restored_to_original": destination_path is None,
                "sha256": record.metadata["sha256"],
                "size": record.metadata["size"],
            }

    def purge_trashed_file(
        self, trash_id: str, expected_sha256: str
    ) -> dict[str, object]:
        """Permanently remove one fully verified item; never accept a batch."""

        self._require_purge_enabled()
        self._validate_trash_id(trash_id)
        with self._global_lock:
            self._recover()
            store = self._ensure_store(create=False)
            if store is None:
                raise self._item_not_found(trash_id)
            item_dir = self._locate_item(store, trash_id)
            record = self._read_record(item_dir, trash_id)
            data, _ = self._read_payload(record)
            self._verify_record_digest(record, data)
            self._validate_expected_sha256(expected_sha256, record.metadata["sha256"])
            if self._path_exists(item_dir / "restore-intent.json"):
                raise TrashError(
                    TRASH_OPERATION_CONFLICT,
                    "restore operation is still pending for this trash item",
                    details={"trash_id": trash_id},
                )

            purging = self._ensure_purging(store)
            purging_item = purging / trash_id
            try:
                purging_item.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise self._storage_error(
                    "cannot inspect purge transaction directory", exc
                ) from exc
            else:
                raise TrashError(
                    TRASH_OPERATION_CONFLICT,
                    "purge transaction already exists for this trash item",
                    details={"trash_id": trash_id},
                )
            try:
                os.replace(item_dir, purging_item)
                self.workspace._sync_directory(store.items)
                self.workspace._sync_directory(purging)
            except OSError as exc:
                raise TrashError(
                    TRASH_OPERATION_CONFLICT,
                    "purge transaction could not be committed",
                    details={"trash_id": trash_id},
                ) from exc

            cleanup_pending = self._cleanup_purging_item(purging_item)
            self.workspace._sync_directory(purging)
            return {
                "status": "purged",
                "purged": True,
                "trash_id": trash_id,
                "original_path": record.metadata["original_path"],
                "sha256": record.metadata["sha256"],
                "size": record.metadata["size"],
                "cleanup_pending": cleanup_pending,
            }

    def _require_enabled(self) -> None:
        if not self.settings.allow_trash:
            raise TrashError(TRASH_DISABLED, "trash is disabled")

    def _require_purge_enabled(self) -> None:
        if not self.settings.allow_trash or not self.settings.allow_trash_purge:
            raise TrashError(TRASH_DISABLED, "trash purge is disabled")

    def _new_trash_id(self, store: _Store) -> str:
        for _ in range(10):
            trash_id = secrets.token_hex(16)
            if not self._path_exists(store.items / trash_id) and not self._path_exists(
                store.staging / trash_id
            ):
                return trash_id
        raise TrashError(
            TRASH_OPERATION_CONFLICT, "could not allocate a unique trash id"
        )

    @overload
    def _ensure_store(self, *, create: Literal[True]) -> _Store: ...

    @overload
    def _ensure_store(self, *, create: Literal[False]) -> _Store | None: ...

    def _ensure_store(self, *, create: bool) -> _Store | None:
        root = self.workspace.root / TRASH_DIRECTORY_NAME
        try:
            root.lstat()
            root_existed = True
        except FileNotFoundError:
            root_existed = False
            if not create:
                return None
            try:
                root.mkdir(mode=0o700)
            except OSError as exc:
                raise self._storage_error(
                    "cannot create protected trash directory", exc
                ) from exc
        except OSError as exc:
            raise self._storage_error(
                "cannot inspect protected trash directory", exc
            ) from exc
        self._require_real_directory(root, "trash directory", TRASH_STORAGE_CORRUPT)

        marker = root / "format.json"
        try:
            marker.lstat()
            marker_exists = True
        except FileNotFoundError:
            marker_exists = False
        except OSError as exc:
            raise self._storage_error(
                "cannot inspect trash format marker", exc
            ) from exc
        if not marker_exists:
            if root_existed:
                raise TrashError(
                    TRASH_STORAGE_CORRUPT,
                    "trash storage format marker is missing",
                )
            self._write_json(
                marker,
                {"version": _FORMAT_VERSION},
                error_code=TRASH_STORAGE_IO_ERROR,
            )
        else:
            marker_data = self._read_json(
                marker, _METADATA_MAX_BYTES, error_code=TRASH_STORAGE_CORRUPT
            )
            if marker_data.get("version") != _FORMAT_VERSION:
                raise TrashError(
                    TRASH_STORAGE_CORRUPT,
                    "unsupported trash storage format marker",
                )

        staging = root / "staging"
        items = root / "items"
        for directory in (staging, items):
            try:
                directory.lstat()
                directory_exists = True
            except FileNotFoundError:
                directory_exists = False
            except OSError as exc:
                raise self._storage_error(
                    "cannot inspect trash storage directory", exc
                ) from exc
            if not directory_exists:
                if not create:
                    raise TrashError(
                        TRASH_STORAGE_CORRUPT,
                        "required trash storage directory is missing",
                    )
                try:
                    directory.mkdir(mode=0o700)
                except OSError as exc:
                    raise self._storage_error(
                        "cannot create protected trash directory", exc
                    ) from exc
            self._require_real_directory(
                directory, "trash storage directory", TRASH_STORAGE_CORRUPT
            )
        return _Store(root=root, staging=staging, items=items, purging=root / "purging")

    def _ensure_purging(self, store: _Store) -> Path:
        try:
            store.purging.lstat()
            exists = True
        except FileNotFoundError:
            exists = False
        except OSError as exc:
            raise self._storage_error(
                "cannot inspect purge transaction directory", exc
            ) from exc
        if not exists:
            try:
                store.purging.mkdir(mode=0o700)
            except OSError as exc:
                raise self._storage_error(
                    "cannot create purge transaction directory", exc
                ) from exc
        self._require_real_directory(
            store.purging, "purge transaction directory", TRASH_STORAGE_CORRUPT
        )
        return store.purging

    def _recover(self) -> list[str]:
        store = self._ensure_store(create=False)
        if store is None:
            return []
        diagnostics: list[str] = []
        try:
            purging_status = store.purging.lstat()
        except FileNotFoundError:
            purging_status = None
        except OSError as exc:
            raise self._storage_error(
                "cannot inspect purge transaction directory", exc
            ) from exc
        if purging_status is not None:
            if stat.S_ISLNK(purging_status.st_mode) or not stat.S_ISDIR(
                purging_status.st_mode
            ):
                raise TrashError(
                    TRASH_STORAGE_CORRUPT,
                    "purge transaction path is not a real directory",
                )
            self._recover_purging(store.purging, diagnostics)
        for trash_id, item_dir in self._named_directories(store.staging, diagnostics):
            self._recover_staging_item(store, trash_id, item_dir, diagnostics)
        for trash_id, item_dir in self._named_directories(store.items, diagnostics):
            self._recover_formal_item(trash_id, item_dir, diagnostics)
        return self._bounded_diagnostics(diagnostics)

    def _recover_purging(self, purging: Path, diagnostics: list[str]) -> None:
        try:
            entries = list(os.scandir(purging))
        except OSError:
            diagnostics.append("cannot scan purge transaction directory")
            return
        for entry in entries[: self.settings.max_trash_items]:
            if _TRASH_ID_PATTERN.fullmatch(entry.name) is None:
                diagnostics.append("invalid purge transaction entry name")
                continue
            item_dir = Path(entry.path)
            try:
                status = item_dir.lstat()
            except OSError:
                diagnostics.append("cannot inspect purge transaction item")
                continue
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                diagnostics.append("purge transaction item is not a real directory")
                continue
            if not self._cleanup_purging_item(item_dir):
                diagnostics.append("purge transaction cleanup is pending")
        if len(entries) > self.settings.max_trash_items:
            diagnostics.append("purge transaction entry limit reached")

    def _recover_staging_item(
        self,
        store: _Store,
        trash_id: str,
        item_dir: Path,
        diagnostics: list[str],
    ) -> None:
        metadata_path = item_dir / "metadata.json"
        payload = item_dir / "payload"
        try:
            metadata_exists = self._path_exists(metadata_path)
            payload_exists = self._path_exists(payload)
            if metadata_exists and payload_exists:
                self._read_record(item_dir, trash_id)
                final_item = store.items / trash_id
                if self._path_exists(final_item):
                    diagnostics.append("staging item conflicts with formal item")
                    return
                os.replace(item_dir, final_item)
                self.workspace._sync_directory(store.items)
                return
            if metadata_exists and not payload_exists:
                metadata = self._read_metadata(metadata_path, trash_id)
                if self._original_matches(metadata):
                    self._remove_empty_item(item_dir, metadata_path)
                else:
                    diagnostics.append("incomplete staging item")
                return
            diagnostics.append("incomplete staging item")
        except (OSError, WorkspaceError):
            diagnostics.append("invalid staging item")

    def _recover_formal_item(
        self, trash_id: str, item_dir: Path, diagnostics: list[str]
    ) -> None:
        metadata_path = item_dir / "metadata.json"
        payload = item_dir / "payload"
        intent_path = item_dir / "restore-intent.json"
        try:
            metadata_exists = self._path_exists(metadata_path)
            payload_exists = self._path_exists(payload)
            intent_exists = self._path_exists(intent_path)
            if not metadata_exists:
                if intent_exists and not payload_exists:
                    self._recover_without_metadata(item_dir, intent_path, diagnostics)
                elif not payload_exists:
                    item_dir.rmdir()
                else:
                    diagnostics.append("formal item has no metadata")
                return
            metadata = self._read_metadata(metadata_path, trash_id)
            if intent_exists:
                self._recover_restore_intent(
                    trash_id, item_dir, metadata, payload_exists, diagnostics
                )
                return
            if not payload_exists:
                if self._original_matches(metadata):
                    self._remove_empty_item(item_dir, metadata_path)
                else:
                    diagnostics.append("formal item is missing its payload")
                return
            record = self._read_record(item_dir, trash_id)
            if self._original_matches(metadata):
                data, _ = self._read_payload(record)
                if hashlib.sha256(data).hexdigest() == metadata["sha256"]:
                    payload.unlink()
                    metadata_path.unlink()
                    item_dir.rmdir()
                    self.workspace._sync_directory(item_dir.parent)
                else:
                    diagnostics.append("formal item payload checksum is invalid")
        except (OSError, WorkspaceError):
            diagnostics.append("invalid formal item")

    def _recover_without_metadata(
        self, item_dir: Path, intent_path: Path, diagnostics: list[str]
    ) -> None:
        try:
            intent = self._read_json(intent_path, _METADATA_MAX_BYTES)
            destination = intent.get("destination_path")
            sha256 = intent.get("sha256")
            if (
                not isinstance(destination, str)
                or not isinstance(sha256, str)
                or _SHA256_PATTERN.fullmatch(sha256) is None
            ):
                diagnostics.append("restore intent is invalid")
                return
            target = self.workspace.safe_restore_target(
                destination, require_absent=False
            )
            if self._target_matches(target, sha256, None):
                intent_path.unlink()
                item_dir.rmdir()
            else:
                diagnostics.append("restore intent has no verifiable target")
        except (OSError, WorkspaceError):
            diagnostics.append("restore intent is invalid")

    def _recover_restore_intent(
        self,
        trash_id: str,
        item_dir: Path,
        metadata: dict[str, Any],
        payload_exists: bool,
        diagnostics: list[str],
    ) -> None:
        intent_path = item_dir / "restore-intent.json"
        try:
            intent = self._read_restore_intent(intent_path, trash_id, metadata)
            target = self.workspace.safe_restore_target(
                intent["destination_path"], require_absent=False
            )
            target_exists = self._path_exists(target)
            if payload_exists:
                record = self._read_record(item_dir, trash_id)
                data, _ = self._read_payload(record)
                if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
                    diagnostics.append("restore intent payload checksum is invalid")
                    return
            if not target_exists:
                if payload_exists:
                    intent_path.unlink()
                else:
                    diagnostics.append("restore intent has neither payload nor target")
                return
            if not self._target_matches(target, metadata["sha256"], metadata["size"]):
                diagnostics.append("restore intent target conflicts with its payload")
                return
            if payload_exists:
                (item_dir / "payload").unlink()
            (item_dir / "metadata.json").unlink()
            intent_path.unlink()
            item_dir.rmdir()
            self.workspace._sync_directory(item_dir.parent)
        except (OSError, WorkspaceError):
            diagnostics.append("restore intent recovery is pending")

    def _load_records(self) -> tuple[list[_Record], list[str]]:
        store = self._ensure_store(create=False)
        if store is None:
            return [], []
        diagnostics: list[str] = []
        records: list[_Record] = []
        for trash_id, item_dir in self._named_directories(store.items, diagnostics):
            try:
                records.append(self._read_record(item_dir, trash_id))
            except (OSError, WorkspaceError):
                diagnostics.append("invalid formal item")
        return records, self._bounded_diagnostics(diagnostics)

    def _named_directories(
        self, parent: Path, diagnostics: list[str]
    ) -> list[tuple[str, Path]]:
        result: list[tuple[str, Path]] = []
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    if len(result) >= self.settings.max_trash_items:
                        diagnostics.append("trash directory entry limit reached")
                        break
                    if _TRASH_ID_PATTERN.fullmatch(entry.name) is None:
                        diagnostics.append("invalid trash entry name")
                        continue
                    path = Path(entry.path)
                    try:
                        status = path.lstat()
                    except OSError:
                        diagnostics.append("cannot inspect trash entry")
                        continue
                    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                        diagnostics.append("trash entry is not a real directory")
                        continue
                    result.append((entry.name, path))
        except OSError:
            diagnostics.append("cannot scan trash directory")
        return result

    def _locate_item(self, store: _Store, trash_id: str) -> Path:
        item_dir = store.items / trash_id
        try:
            status = item_dir.lstat()
        except FileNotFoundError:
            raise self._item_not_found(trash_id) from None
        except PermissionError as exc:
            raise TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
                details={"trash_id": trash_id},
            ) from exc
        except OSError as exc:
            raise TrashError(
                TRASH_STORAGE_IO_ERROR,
                "cannot inspect trash storage",
                details={"trash_id": trash_id},
            ) from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash item is not a real directory",
                details={"trash_id": trash_id},
            )
        return item_dir

    def _read_record(self, item_dir: Path, trash_id: str) -> _Record:
        self._require_real_directory(
            item_dir, "trash item directory", TRASH_ITEM_CORRUPT
        )
        self._validate_item_contents(item_dir, trash_id)
        metadata = self._read_metadata(item_dir / "metadata.json", trash_id)
        intent_path = item_dir / "restore-intent.json"
        if self._path_exists(intent_path):
            self._read_restore_intent(intent_path, trash_id, metadata)
        payload = item_dir / "payload"
        try:
            payload_status = payload.lstat()
        except FileNotFoundError as exc:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash payload is missing",
                details={"trash_id": trash_id},
            ) from exc
        except PermissionError as exc:
            raise TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
                details={"trash_id": trash_id},
            ) from exc
        except OSError as exc:
            raise TrashError(
                TRASH_STORAGE_IO_ERROR,
                "cannot inspect trash payload",
                details={"trash_id": trash_id},
            ) from exc
        if stat.S_ISLNK(payload_status.st_mode) or not stat.S_ISREG(
            payload_status.st_mode
        ):
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash payload is not a regular file",
                details={"trash_id": trash_id},
            )
        if payload_status.st_size != metadata["size"]:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash payload size does not match metadata",
                details={"trash_id": trash_id},
            )
        return _Record(metadata=metadata, item_dir=item_dir, payload=payload)

    def _validate_item_contents(self, item_dir: Path, trash_id: str) -> None:
        try:
            entries = list(os.scandir(item_dir))
        except PermissionError as exc:
            raise TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
                details={"trash_id": trash_id},
            ) from exc
        except OSError as exc:
            raise TrashError(
                TRASH_STORAGE_IO_ERROR,
                "cannot inspect trash item",
                details={"trash_id": trash_id},
            ) from exc
        for entry in entries:
            if entry.name not in _ITEM_FILES:
                raise TrashError(
                    TRASH_ITEM_CORRUPT,
                    "trash item contains an unknown file",
                    details={"trash_id": trash_id},
                )
            try:
                status = Path(entry.path).lstat()
            except OSError as exc:
                raise self._storage_error(
                    "cannot inspect trash item", exc, {"trash_id": trash_id}
                ) from exc
            if stat.S_ISLNK(status.st_mode):
                raise TrashError(
                    TRASH_ITEM_CORRUPT,
                    "trash item contains a symbolic link",
                    details={"trash_id": trash_id},
                )

    def _read_metadata(self, path: Path, trash_id: str) -> dict[str, Any]:
        data = self._read_json(
            path, _METADATA_MAX_BYTES, details={"trash_id": trash_id}
        )
        if data.get("version") != _FORMAT_VERSION:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "unsupported trash metadata version",
                details={"trash_id": trash_id},
            )
        if data.get("trash_id") != trash_id:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash metadata id does not match its directory",
                details={"trash_id": trash_id},
            )
        original = data.get("original_path")
        if (
            not isinstance(original, str)
            or not original
            or original in {".", ".."}
            or original.startswith(("/", "\\", "~"))
            or "\\" in original
            or "\x00" in original
            or PureWindowsPath(original).drive
            or any(character in original for character in "*?[]{}")
            or any(part in {"", ".", ".."} for part in original.split("/"))
        ):
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash metadata has an invalid original_path",
                details={"trash_id": trash_id},
            )
        sha256 = data.get("sha256")
        if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash metadata has an invalid sha256",
                details={"trash_id": trash_id},
            )
        for name in ("size", "mtime_ns", "mode", "trashed_at"):
            value = data.get(name)
            if type(value) is not int or value < 0:
                raise TrashError(
                    TRASH_ITEM_CORRUPT,
                    f"trash metadata has an invalid {name}",
                    details={"trash_id": trash_id},
                )
        if data["size"] > self.settings.max_file_size:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash metadata size exceeds the workspace file limit",
                details={"trash_id": trash_id},
            )
        if data["mode"] > 0o7777 or data["trashed_at"] == 0:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash metadata has an invalid mode or timestamp",
                details={"trash_id": trash_id},
            )
        return data

    def _read_restore_intent(
        self,
        path: Path,
        trash_id: str,
        metadata: dict[str, Any],
    ) -> _RestoreIntent:
        value = self._read_json(
            path, _METADATA_MAX_BYTES, details={"trash_id": trash_id}
        )
        if set(value) != {"version", "trash_id", "destination_path", "sha256"}:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "restore intent has an invalid schema",
                details={"trash_id": trash_id},
            )
        destination = value.get("destination_path")
        if (
            value.get("version") != 1
            or value.get("trash_id") != trash_id
            or value.get("sha256") != metadata["sha256"]
            or not isinstance(destination, str)
        ):
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "restore intent does not match metadata",
                details={"trash_id": trash_id},
            )
        try:
            self.workspace.safe_restore_target(destination, require_absent=False)
        except WorkspaceError as exc:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "restore intent destination is invalid",
                details={"trash_id": trash_id},
            ) from exc
        return {
            "version": 1,
            "trash_id": trash_id,
            "destination_path": destination,
            "sha256": metadata["sha256"],
        }

    def _read_json(
        self,
        path: Path,
        max_bytes: int,
        *,
        error_code: str = TRASH_ITEM_CORRUPT,
        details: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        try:
            status = path.lstat()
        except FileNotFoundError as exc:
            raise TrashError(
                error_code, "trash metadata is missing", details=details
            ) from exc
        except PermissionError as exc:
            raise TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
                details=details,
            ) from exc
        except OSError as exc:
            raise TrashError(
                TRASH_STORAGE_IO_ERROR,
                "cannot inspect trash metadata",
                details=details,
            ) from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise TrashError(
                error_code,
                "trash metadata is not a regular file",
                details=details,
            )
        if status.st_size > max_bytes:
            raise TrashError(error_code, "trash metadata is too large", details=details)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                raw = handle.read(max_bytes + 1)
        except PermissionError as exc:
            raise TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
                details=details,
            ) from exc
        except OSError as exc:
            raise TrashError(
                TRASH_STORAGE_IO_ERROR,
                "cannot read trash metadata",
                details=details,
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(raw) > max_bytes:
            raise TrashError(error_code, "trash metadata is too large", details=details)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TrashError(
                error_code, "trash metadata is not valid UTF-8 JSON", details=details
            ) from exc
        if not isinstance(value, dict):
            raise TrashError(
                error_code, "trash metadata must be a JSON object", details=details
            )
        return value

    def _read_payload(self, record: _Record) -> tuple[bytes, os.stat_result]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = -1
        try:
            descriptor = os.open(record.payload, flags)
        except PermissionError as exc:
            raise TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
                details={"trash_id": record.metadata["trash_id"]},
            ) from exc
        except OSError as exc:
            raise TrashError(
                TRASH_STORAGE_IO_ERROR,
                "cannot open trash payload safely",
                details={"trash_id": record.metadata["trash_id"]},
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise TrashError(
                    TRASH_ITEM_CORRUPT,
                    "trash payload is not a regular file",
                    details={"trash_id": record.metadata["trash_id"]},
                )
            if before.st_size > self.settings.max_file_size:
                raise TrashError(
                    TRASH_ITEM_CORRUPT,
                    "trash payload exceeds the workspace file limit",
                    details={"trash_id": record.metadata["trash_id"]},
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(self.settings.max_file_size + 1)
                after = os.fstat(handle.fileno())
        except PermissionError as exc:
            raise TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
                details={"trash_id": record.metadata["trash_id"]},
            ) from exc
        except OSError as exc:
            raise TrashError(
                TRASH_STORAGE_IO_ERROR,
                "cannot read trash payload safely",
                details={"trash_id": record.metadata["trash_id"]},
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(data) > self.settings.max_file_size:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash payload exceeds the workspace file limit",
                details={"trash_id": record.metadata["trash_id"]},
            )
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(data) != after.st_size:
            raise TrashError(
                TRASH_ITEM_CORRUPT,
                "trash payload changed while it was being read",
                details={"trash_id": record.metadata["trash_id"]},
            )
        return data, after

    def _verify_record_digest(self, record: _Record, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        if digest != record.metadata["sha256"] or len(data) != record.metadata["size"]:
            raise TrashError(
                TRASH_VERSION_CONFLICT,
                "trash payload checksum does not match metadata",
                details={"trash_id": record.metadata["trash_id"]},
            )

    def _original_matches(self, metadata: dict[str, Any]) -> bool:
        try:
            original = self.workspace.safe_regular_file_path(metadata["original_path"])
            with self.workspace._lock_for_path(original):
                _, state = self.workspace._read_bytes_and_state(original)
        except WorkspaceError:
            return False
        return state.sha256 == metadata["sha256"] and state.size == metadata["size"]

    def _restore_target(
        self, metadata: dict[str, Any], destination_path: str | None
    ) -> tuple[Path, str]:
        requested = (
            metadata["original_path"] if destination_path is None else destination_path
        )
        try:
            target = self.workspace.safe_restore_target(requested)
        except RestoreTargetError as exc:
            code = (
                TRASH_DESTINATION_EXISTS
                if exc.reason == "exists"
                else TRASH_DESTINATION_INVALID
            )
            raise TrashError(
                code,
                "restore destination already exists"
                if code == TRASH_DESTINATION_EXISTS
                else "restore destination is invalid",
                details={"restored_path": requested}
                if code == TRASH_DESTINATION_EXISTS
                else None,
            ) from exc
        except (TypeError, ValueError, WorkspaceError) as exc:
            raise TrashError(
                TRASH_DESTINATION_INVALID,
                "restore destination is invalid",
            ) from exc
        return target, self.workspace.relative_path(target)

    def _target_matches(
        self, target: Path, expected_sha256: str, expected_size: int | None
    ) -> bool:
        with self.workspace._lock_for_path(target):
            try:
                status = target.lstat()
                if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                    return False
                relative = self.workspace.relative_path(target)
                _, state = self.workspace._read_bytes_and_state(
                    self.workspace.safe_regular_file_path(relative)
                )
            except WorkspaceError:
                return False
        return state.sha256 == expected_sha256 and (
            expected_size is None or state.size == expected_size
        )

    def _write_json(
        self,
        path: Path,
        value: dict[str, object],
        *,
        error_code: str = TRASH_ITEM_CORRUPT,
    ) -> None:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _METADATA_MAX_BYTES:
            raise TrashError(error_code, "trash metadata is too large")
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._chmod_file(temporary, 0o600)
            os.replace(temporary, path)
            self.workspace._sync_directory(path.parent)
        except PermissionError as exc:
            raise TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
            ) from exc
        except OSError as exc:
            raise TrashError(
                TRASH_STORAGE_IO_ERROR,
                "cannot write trash metadata",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _chmod_file(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode, follow_symlinks=False)
        except (NotImplementedError, TypeError):
            os.chmod(path, mode)

    @staticmethod
    def _require_real_directory(path: Path, description: str, error_code: str) -> None:
        try:
            status = path.lstat()
        except FileNotFoundError as exc:
            raise TrashError(error_code, f"{description} is missing") from exc
        except PermissionError as exc:
            raise TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
            ) from exc
        except OSError as exc:
            raise TrashError(
                TRASH_STORAGE_IO_ERROR,
                f"cannot inspect {description}",
            ) from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise TrashError(error_code, f"{description} is not a real directory")

    def _cleanup_purging_item(self, item_dir: Path) -> bool:
        pending = False
        try:
            entries = list(os.scandir(item_dir))
        except OSError:
            return True
        for entry in entries:
            if entry.name not in _ITEM_FILES:
                pending = True
                continue
            path = Path(entry.path)
            try:
                status = path.lstat()
                if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                    pending = True
                    continue
                path.unlink()
            except OSError:
                pending = True
        if not pending:
            try:
                item_dir.rmdir()
            except OSError:
                pending = True
        return pending

    @staticmethod
    def _remove_empty_item(item_dir: Path, metadata_path: Path) -> None:
        metadata_path.unlink()
        item_dir.rmdir()

    @staticmethod
    def _public_metadata(metadata: dict[str, Any]) -> dict[str, object]:
        return {
            name: metadata[name]
            for name in (
                "trash_id",
                "original_path",
                "sha256",
                "size",
                "mtime_ns",
                "mode",
                "trashed_at",
            )
        }

    @staticmethod
    def _bounded_diagnostics(diagnostics: list[str]) -> list[str]:
        unique = list(dict.fromkeys(diagnostics))
        if len(unique) > _MAX_DIAGNOSTICS:
            return [
                *unique[:_MAX_DIAGNOSTICS],
                "... more trash diagnostics omitted ...",
            ]
        return unique

    def _fit_list_output(self, result: dict[str, object]) -> None:
        items = result["items"]
        if not isinstance(items, list):
            return
        while items and self._encoded_size(result) > self.settings.max_output_size:
            items.pop()
            result["has_more"] = True
        if self._encoded_size(result) > self.settings.max_output_size:
            result.pop("diagnostics", None)

    @staticmethod
    def _encoded_size(result: dict[str, object]) -> int:
        return len(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        )

    @staticmethod
    def _validate_trash_id(trash_id: str) -> None:
        if (
            not isinstance(trash_id, str)
            or _TRASH_ID_PATTERN.fullmatch(trash_id) is None
        ):
            raise TrashError(
                TRASH_ID_INVALID,
                "trash_id must be a 32-character lowercase hex id",
            )

    @staticmethod
    def _validate_expected_sha256(expected: str, actual: str) -> None:
        if (
            not isinstance(expected, str)
            or _SHA256_PATTERN.fullmatch(expected.casefold()) is None
        ):
            raise TrashError(
                TRASH_VERSION_CONFLICT,
                "expected_sha256 must be a 64-character hex digest",
            )
        if expected.casefold() != actual:
            raise TrashError(
                TRASH_VERSION_CONFLICT,
                "conflict: trash item changed; list it again and retry",
            )

    @staticmethod
    def _reject_glob(path: str) -> None:
        if not isinstance(path, str):
            raise TrashError(TRASH_DESTINATION_INVALID, "path must be a string")
        if any(character in path for character in "*?[]{}"):
            raise TrashError(
                TRASH_DESTINATION_INVALID,
                "wildcard and glob paths are not supported",
            )

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _item_not_found(trash_id: str) -> TrashError:
        return TrashError(
            TRASH_ITEM_NOT_FOUND,
            "trash item not found",
            details={"trash_id": trash_id},
        )

    @staticmethod
    def _storage_error(
        message: str,
        exc: OSError,
        details: dict[str, object] | None = None,
    ) -> TrashError:
        if isinstance(exc, PermissionError):
            return TrashError(
                TRASH_STORAGE_PERMISSION_DENIED,
                "trash storage permission denied",
                details=details,
            )
        return TrashError(TRASH_STORAGE_IO_ERROR, message, details=details)

    @staticmethod
    def _remove_intent_best_effort(item_dir: Path) -> None:
        try:
            item_dir.joinpath("restore-intent.json").unlink()
        except OSError:
            pass
