"""Execution record/event stores for in-memory and SQLite persistence."""

from __future__ import annotations

import sqlite3
import stat
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .artifact import ArtifactRecord
from .execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionReason,
    ExecutionRecord,
    ExecutionResources,
    ExecutionState,
    ensure_execution_transition,
    public_execution_error_summary,
)

_SCHEMA_VERSION = 4
_MAX_EVENT_PAGE_SIZE = 100
_MAX_ARTIFACT_MANIFEST_ITEMS = 100
DEFAULT_MAX_RETAINED_TERMINAL_EXECUTIONS = 1024
_UNFINISHED_STATES = frozenset(
    {
        ExecutionState.STARTING,
        ExecutionState.RUNNING,
        ExecutionState.CANCELLING,
    }
)
_EXECUTION_COLUMNS = (
    "execution_id",
    "kind",
    "name",
    "tool",
    "mode",
    "state",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "exit_code",
    "reason",
    "error_summary",
    "wall_time_ms",
    "cpu_time_ms",
    "peak_memory_bytes",
    "workspace_initial_bytes",
    "workspace_final_bytes",
    "workspace_growth_bytes",
    "stdout_bytes",
    "stderr_bytes",
    "output_bytes",
)
_EVENT_COLUMNS = (
    "execution_id",
    "sequence",
    "event_type",
    "timestamp",
    "from_state",
    "to_state",
    "reason",
    "error_summary",
)
_ARTIFACT_COLUMNS = (
    "artifact_id",
    "execution_id",
    "name",
    "media_type",
    "size_bytes",
    "sha256",
    "created_at",
)
_ARTIFACT_MANIFEST_COLUMNS = ("execution_id", "complete")


class ExecutionStoreError(RuntimeError):
    """Base class for fail-closed execution persistence errors."""


class DuplicateExecutionError(ExecutionStoreError):
    """Raised when a manager-issued execution ID already exists."""


class UnknownExecutionError(ExecutionStoreError):
    """Raised when an execution ID is not present in the store."""


class ExecutionConflictError(ExecutionStoreError):
    """Raised when optimistic state checking rejects a transition."""


@dataclass(frozen=True, slots=True)
class ExecutionEventPage:
    events: list[ExecutionEvent]
    next_cursor: int
    has_more: bool
    history_complete: bool


@dataclass(frozen=True, slots=True)
class ExecutionArtifactManifest:
    artifacts: list[ArtifactRecord]
    manifest_complete: bool


class ExecutionStore(Protocol):
    """Persistence boundary for canonical execution records and lifecycle events."""

    def create(self, record: ExecutionRecord) -> None: ...

    def get(self, execution_id: str) -> ExecutionRecord: ...

    def transition(
        self,
        execution_id: str,
        expected_states: Iterable[ExecutionState],
        new_state: ExecutionState,
        *,
        reason: ExecutionReason | None = None,
        exit_code: int | None = None,
        resources: ExecutionResources | None = None,
        artifact_manifest: Iterable[ArtifactRecord] | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        updated_at: float | None = None,
    ) -> ExecutionRecord: ...

    def request_cancellation(
        self,
        execution_id: str,
        expected_states: Iterable[ExecutionState],
        reason: ExecutionReason,
    ) -> ExecutionRecord: ...

    def list_unfinished(self) -> list[ExecutionRecord]: ...

    def list_events(
        self, execution_id: str, *, cursor: int = 0, limit: int = 50
    ) -> ExecutionEventPage: ...

    def list_artifacts(self, execution_id: str) -> ExecutionArtifactManifest: ...


class InMemoryExecutionStore:
    """Thread-safe bounded process-local execution record and audit history."""

    def __init__(
        self,
        max_retained_terminal_executions: int = (
            DEFAULT_MAX_RETAINED_TERMINAL_EXECUTIONS
        ),
    ) -> None:
        if (
            type(max_retained_terminal_executions) is not int
            or max_retained_terminal_executions <= 0
        ):
            raise ValueError(
                "max_retained_terminal_executions must be a positive integer"
            )
        self.max_retained_terminal_executions = max_retained_terminal_executions
        self._records: dict[str, ExecutionRecord] = {}
        self._events: dict[str, list[ExecutionEvent]] = {}
        self._artifact_manifests: dict[str, list[ArtifactRecord]] = {}
        self._artifact_manifest_complete: dict[str, bool] = {}
        self._terminal_order: deque[str] = deque()
        self._lock = threading.RLock()

    def create(self, record: ExecutionRecord) -> None:
        event = _created_event(record)
        with self._lock:
            if record.execution_id in self._records:
                raise DuplicateExecutionError(
                    f"duplicate execution_id: {record.execution_id}"
                )
            self._records[record.execution_id] = record
            self._events[record.execution_id] = [event]
            self._artifact_manifests[record.execution_id] = []
            self._artifact_manifest_complete[record.execution_id] = False

    def get(self, execution_id: str) -> ExecutionRecord:
        with self._lock:
            try:
                return self._records[execution_id]
            except KeyError as exc:
                raise UnknownExecutionError(
                    f"unknown execution_id: {execution_id}"
                ) from exc

    def transition(
        self,
        execution_id: str,
        expected_states: Iterable[ExecutionState],
        new_state: ExecutionState,
        *,
        reason: ExecutionReason | None = None,
        exit_code: int | None = None,
        resources: ExecutionResources | None = None,
        artifact_manifest: Iterable[ArtifactRecord] | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        updated_at: float | None = None,
    ) -> ExecutionRecord:
        expected = frozenset(expected_states)
        if not expected:
            raise ValueError("expected_states must not be empty")
        with self._lock:
            try:
                current = self._records[execution_id]
            except KeyError as exc:
                raise UnknownExecutionError(
                    f"unknown execution_id: {execution_id}"
                ) from exc
            if current.state not in expected:
                raise ExecutionConflictError(
                    f"execution {execution_id} is {current.state.value}, "
                    "not an expected state"
                )
            ensure_execution_transition(current.state, new_state)
            record = _transitioned_record(
                current,
                new_state,
                reason=reason,
                exit_code=exit_code,
                resources=resources,
                started_at=started_at,
                finished_at=finished_at,
                updated_at=updated_at,
            )
            manifest = _normalize_artifact_manifest(
                execution_id, artifact_manifest, terminal=record.terminal
            )
            events = self._events[execution_id]
            event = _transition_event(current, record, len(events) + 1)
            self._records[execution_id] = record
            events.append(event)
            if manifest is not None:
                self._artifact_manifests[execution_id] = list(manifest)
                self._artifact_manifest_complete[execution_id] = True
            if record.terminal:
                self._terminal_order.append(execution_id)
                self._prune_terminal_locked()
            return record

    def request_cancellation(
        self,
        execution_id: str,
        expected_states: Iterable[ExecutionState],
        reason: ExecutionReason,
    ) -> ExecutionRecord:
        expected = frozenset(expected_states)
        if not expected:
            raise ValueError("expected_states must not be empty")
        with self._lock:
            try:
                current = self._records[execution_id]
            except KeyError as exc:
                raise UnknownExecutionError(
                    f"unknown execution_id: {execution_id}"
                ) from exc
            if current.state not in expected:
                raise ExecutionConflictError(
                    f"execution {execution_id} is {current.state.value}, "
                    "not an expected state"
                )
            ensure_execution_transition(current.state, ExecutionState.CANCELLING)
            timestamp = time.time()
            updated = _transitioned_record(
                current,
                ExecutionState.CANCELLING,
                reason=reason,
                exit_code=None,
                resources=None,
                started_at=None,
                finished_at=None,
                updated_at=timestamp,
            )
            events = self._events[execution_id]
            requested = _cancellation_requested_event(
                current, reason, len(events) + 1, timestamp
            )
            transitioned = _transition_event(current, updated, len(events) + 2)
            self._records[execution_id] = updated
            events.extend((requested, transitioned))
            return updated

    def _prune_terminal_locked(self) -> None:
        while len(self._terminal_order) > self.max_retained_terminal_executions:
            execution_id = self._terminal_order.popleft()
            record = self._records.get(execution_id)
            if record is None or not record.terminal:
                continue
            self._records.pop(execution_id, None)
            self._events.pop(execution_id, None)
            self._artifact_manifests.pop(execution_id, None)
            self._artifact_manifest_complete.pop(execution_id, None)

    def list_unfinished(self) -> list[ExecutionRecord]:
        with self._lock:
            return [
                record
                for record in self._records.values()
                if record.state in _UNFINISHED_STATES
            ]

    def list_events(
        self, execution_id: str, *, cursor: int = 0, limit: int = 50
    ) -> ExecutionEventPage:
        _validate_event_page_args(cursor, limit)
        with self._lock:
            if execution_id not in self._records:
                raise UnknownExecutionError(f"unknown execution_id: {execution_id}")
            record = self._records[execution_id]
            all_events = self._events[execution_id]
            history_complete = _validate_event_history(record, all_events)
            selected = [event for event in all_events if event.sequence > cursor]
            page = selected[: limit + 1]
            has_more = len(page) > limit
            events = page[:limit]
            next_cursor = events[-1].sequence if events else cursor
            return ExecutionEventPage(
                events=list(events),
                next_cursor=next_cursor,
                has_more=has_more,
                history_complete=history_complete,
            )

    def list_artifacts(self, execution_id: str) -> ExecutionArtifactManifest:
        with self._lock:
            if execution_id not in self._records:
                raise UnknownExecutionError(f"unknown execution_id: {execution_id}")
            return ExecutionArtifactManifest(
                artifacts=list(self._artifact_manifests[execution_id]),
                manifest_complete=self._artifact_manifest_complete[execution_id],
            )


class SqliteExecutionStore:
    """SQLite-backed durable execution records and append-only lifecycle events."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialize()

    def create(self, record: ExecutionRecord) -> None:
        event = _created_event(record)
        placeholders = ", ".join("?" for _ in _EXECUTION_COLUMNS)
        sql = (
            f"INSERT INTO executions ({', '.join(_EXECUTION_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(sql, _record_values(record))
                except sqlite3.IntegrityError as exc:
                    raise DuplicateExecutionError(
                        f"duplicate execution_id: {record.execution_id}"
                    ) from exc
                connection.execute(
                    f"INSERT INTO execution_events ({', '.join(_EVENT_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)})",
                    _event_values(event),
                )
                connection.execute(
                    "INSERT INTO execution_artifact_manifests "
                    "(execution_id, complete) VALUES (?, 0)",
                    (record.execution_id,),
                )
                connection.commit()
        except DuplicateExecutionError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ExecutionStoreError(f"cannot create execution record: {exc}") from exc

    def get(self, execution_id: str) -> ExecutionRecord:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    f"SELECT {', '.join(_EXECUTION_COLUMNS)} "
                    "FROM executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ExecutionStoreError(f"cannot read execution record: {exc}") from exc
        if row is None:
            raise UnknownExecutionError(f"unknown execution_id: {execution_id}")
        return _record_from_row(row)

    def transition(
        self,
        execution_id: str,
        expected_states: Iterable[ExecutionState],
        new_state: ExecutionState,
        *,
        reason: ExecutionReason | None = None,
        exit_code: int | None = None,
        resources: ExecutionResources | None = None,
        artifact_manifest: Iterable[ArtifactRecord] | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        updated_at: float | None = None,
    ) -> ExecutionRecord:
        expected = frozenset(expected_states)
        if not expected:
            raise ValueError("expected_states must not be empty")
        expected_values = tuple(state.value for state in expected)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    f"SELECT {', '.join(_EXECUTION_COLUMNS)} "
                    "FROM executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                if row is None:
                    raise UnknownExecutionError(f"unknown execution_id: {execution_id}")
                current = _record_from_row(row)
                if current.state not in expected:
                    raise ExecutionConflictError(
                        f"execution {execution_id} is {current.state.value}, "
                        "not an expected state"
                    )
                ensure_execution_transition(current.state, new_state)
                updated = _transitioned_record(
                    current,
                    new_state,
                    reason=reason,
                    exit_code=exit_code,
                    resources=resources,
                    started_at=started_at,
                    finished_at=finished_at,
                    updated_at=updated_at,
                )
                manifest = _normalize_artifact_manifest(
                    execution_id, artifact_manifest, terminal=updated.terminal
                )
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 "
                        "FROM execution_events WHERE execution_id = ?",
                        (execution_id,),
                    ).fetchone()[0]
                )
                event = _transition_event(current, updated, sequence)
                placeholders = ", ".join("?" for _ in expected_values)
                cursor = connection.execute(
                    "UPDATE executions SET "
                    "state = ?, updated_at = ?, started_at = ?, finished_at = ?, "
                    "exit_code = ?, reason = ?, error_summary = ?, "
                    "wall_time_ms = ?, cpu_time_ms = ?, peak_memory_bytes = ?, "
                    "workspace_initial_bytes = ?, workspace_final_bytes = ?, "
                    "workspace_growth_bytes = ?, stdout_bytes = ?, stderr_bytes = ?, "
                    "output_bytes = ? "
                    f"WHERE execution_id = ? AND state IN ({placeholders})",
                    (
                        updated.state.value,
                        updated.updated_at,
                        updated.started_at,
                        updated.finished_at,
                        updated.exit_code,
                        updated.reason.value if updated.reason is not None else None,
                        updated.error_summary,
                        *_resource_values(updated.resources),
                        execution_id,
                        *expected_values,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ExecutionConflictError(
                        f"execution {execution_id} changed during transition"
                    )
                connection.execute(
                    f"INSERT INTO execution_events ({', '.join(_EVENT_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)})",
                    _event_values(event),
                )
                if manifest is not None:
                    connection.execute(
                        "DELETE FROM execution_artifacts WHERE execution_id = ?",
                        (execution_id,),
                    )
                    if manifest:
                        connection.executemany(
                            f"INSERT INTO execution_artifacts "
                            f"({', '.join(_ARTIFACT_COLUMNS)}) "
                            f"VALUES ({', '.join('?' for _ in _ARTIFACT_COLUMNS)})",
                            [_artifact_values(item) for item in manifest],
                        )
                    connection.execute(
                        "UPDATE execution_artifact_manifests SET complete = 1 "
                        "WHERE execution_id = ?",
                        (execution_id,),
                    )
                connection.commit()
                return updated
        except (UnknownExecutionError, ExecutionConflictError):
            raise
        except sqlite3.DatabaseError as exc:
            raise ExecutionStoreError(
                f"cannot transition execution record: {exc}"
            ) from exc

    def request_cancellation(
        self,
        execution_id: str,
        expected_states: Iterable[ExecutionState],
        reason: ExecutionReason,
    ) -> ExecutionRecord:
        expected = frozenset(expected_states)
        if not expected:
            raise ValueError("expected_states must not be empty")
        expected_values = tuple(state.value for state in expected)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    f"SELECT {', '.join(_EXECUTION_COLUMNS)} "
                    "FROM executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                if row is None:
                    raise UnknownExecutionError(f"unknown execution_id: {execution_id}")
                current = _record_from_row(row)
                if current.state not in expected:
                    raise ExecutionConflictError(
                        f"execution {execution_id} is {current.state.value}, "
                        "not an expected state"
                    )
                ensure_execution_transition(current.state, ExecutionState.CANCELLING)
                timestamp = time.time()
                updated = _transitioned_record(
                    current,
                    ExecutionState.CANCELLING,
                    reason=reason,
                    exit_code=None,
                    resources=None,
                    started_at=None,
                    finished_at=None,
                    updated_at=timestamp,
                )
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 "
                        "FROM execution_events WHERE execution_id = ?",
                        (execution_id,),
                    ).fetchone()[0]
                )
                requested = _cancellation_requested_event(
                    current, reason, sequence, timestamp
                )
                transitioned = _transition_event(current, updated, sequence + 1)
                placeholders = ", ".join("?" for _ in expected_values)
                cursor = connection.execute(
                    "UPDATE executions SET "
                    "state = ?, updated_at = ?, started_at = ?, finished_at = ?, "
                    "exit_code = ?, reason = ?, error_summary = ?, "
                    "wall_time_ms = ?, cpu_time_ms = ?, peak_memory_bytes = ?, "
                    "workspace_initial_bytes = ?, workspace_final_bytes = ?, "
                    "workspace_growth_bytes = ?, stdout_bytes = ?, stderr_bytes = ?, "
                    "output_bytes = ? "
                    f"WHERE execution_id = ? AND state IN ({placeholders})",
                    (
                        updated.state.value,
                        updated.updated_at,
                        updated.started_at,
                        updated.finished_at,
                        updated.exit_code,
                        updated.reason.value if updated.reason is not None else None,
                        updated.error_summary,
                        *_resource_values(updated.resources),
                        execution_id,
                        *expected_values,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ExecutionConflictError(
                        f"execution {execution_id} changed during cancellation request"
                    )
                insert_sql = (
                    f"INSERT INTO execution_events ({', '.join(_EVENT_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)})"
                )
                connection.execute(insert_sql, _event_values(requested))
                connection.execute(insert_sql, _event_values(transitioned))
                connection.commit()
                return updated
        except (UnknownExecutionError, ExecutionConflictError):
            raise
        except sqlite3.DatabaseError as exc:
            raise ExecutionStoreError(
                f"cannot persist execution cancellation request: {exc}"
            ) from exc

    def list_unfinished(self) -> list[ExecutionRecord]:
        states = tuple(state.value for state in _UNFINISHED_STATES)
        placeholders = ", ".join("?" for _ in states)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT {', '.join(_EXECUTION_COLUMNS)} FROM executions "
                    f"WHERE state IN ({placeholders}) ORDER BY created_at",
                    states,
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ExecutionStoreError(
                f"cannot list unfinished executions: {exc}"
            ) from exc
        return [_record_from_row(row) for row in rows]

    def list_events(
        self, execution_id: str, *, cursor: int = 0, limit: int = 50
    ) -> ExecutionEventPage:
        _validate_event_page_args(cursor, limit)
        try:
            with self._connect() as connection:
                record_row = connection.execute(
                    f"SELECT {', '.join(_EXECUTION_COLUMNS)} "
                    "FROM executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                if record_row is None:
                    raise UnknownExecutionError(f"unknown execution_id: {execution_id}")
                rows = connection.execute(
                    f"SELECT {', '.join(_EVENT_COLUMNS)} FROM execution_events "
                    "WHERE execution_id = ? ORDER BY sequence",
                    (execution_id,),
                ).fetchall()
        except UnknownExecutionError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ExecutionStoreError(f"cannot list execution events: {exc}") from exc
        record = _record_from_row(record_row)
        all_events = [_event_from_row(row) for row in rows]
        history_complete = _validate_event_history(record, all_events)
        selected = [event for event in all_events if event.sequence > cursor]
        page = selected[: limit + 1]
        events = page[:limit]
        return ExecutionEventPage(
            events=events,
            next_cursor=events[-1].sequence if events else cursor,
            has_more=len(page) > limit,
            history_complete=history_complete,
        )

    def list_artifacts(self, execution_id: str) -> ExecutionArtifactManifest:
        try:
            with self._connect() as connection:
                record_row = connection.execute(
                    f"SELECT {', '.join(_EXECUTION_COLUMNS)} "
                    "FROM executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                if record_row is None:
                    raise UnknownExecutionError(f"unknown execution_id: {execution_id}")
                manifest_row = connection.execute(
                    "SELECT complete FROM execution_artifact_manifests "
                    "WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                if manifest_row is None:
                    raise ExecutionStoreError(
                        "execution database is missing artifact manifest metadata"
                    )
                rows = connection.execute(
                    f"SELECT {', '.join(_ARTIFACT_COLUMNS)} "
                    "FROM execution_artifacts WHERE execution_id = ? "
                    "ORDER BY created_at, artifact_id",
                    (execution_id,),
                ).fetchall()
        except (UnknownExecutionError, ExecutionStoreError):
            raise
        except sqlite3.DatabaseError as exc:
            raise ExecutionStoreError(
                f"cannot list execution artifact manifest: {exc}"
            ) from exc
        record = _record_from_row(record_row)
        complete = manifest_row[0]
        if complete not in (0, 1):
            raise ExecutionStoreError(
                "execution database contains invalid artifact manifest metadata"
            )
        artifacts = [_artifact_from_row(row) for row in rows]
        if len(artifacts) > _MAX_ARTIFACT_MANIFEST_ITEMS:
            raise ExecutionStoreError(
                "execution database contains an oversized artifact manifest"
            )
        if not complete and artifacts:
            raise ExecutionStoreError(
                "execution database contains artifacts for an incomplete manifest"
            )
        if complete and not record.terminal:
            raise ExecutionStoreError(
                "execution database contains a complete manifest "
                "for unfinished execution"
            )
        return ExecutionArtifactManifest(
            artifacts=artifacts,
            manifest_complete=bool(complete),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > _SCHEMA_VERSION:
                    raise ExecutionStoreError(
                        f"execution database version {version} is newer than supported "
                        f"version {_SCHEMA_VERSION}"
                    )
                if version == 0:
                    connection.execute("BEGIN IMMEDIATE")
                    _create_v1_schema(connection)
                    _create_event_schema(connection)
                    _add_resource_columns(connection)
                    _create_artifact_manifest_schema(connection)
                    connection.execute("PRAGMA user_version=4")
                    connection.commit()
                elif version == 1:
                    connection.execute("BEGIN IMMEDIATE")
                    _create_event_schema(connection)
                    _add_resource_columns(connection)
                    _scrub_legacy_error_summaries(connection)
                    _create_artifact_manifest_schema(connection)
                    connection.execute("PRAGMA user_version=4")
                    connection.commit()
                elif version == 2:
                    connection.execute("BEGIN IMMEDIATE")
                    _add_resource_columns(connection)
                    _scrub_legacy_error_summaries(connection)
                    _create_artifact_manifest_schema(connection)
                    connection.execute("PRAGMA user_version=4")
                    connection.commit()
                elif version == 3:
                    connection.execute("BEGIN IMMEDIATE")
                    _scrub_legacy_error_summaries(connection)
                    _create_artifact_manifest_schema(connection)
                    connection.execute("PRAGMA user_version=4")
                    connection.commit()
                self._validate_schema(connection)
        except ExecutionStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ExecutionStoreError(
                f"cannot initialize execution database: {exc}"
            ) from exc

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != _SCHEMA_VERSION:
            raise ExecutionStoreError(
                f"unsupported execution database version: {version}"
            )
        execution_rows = connection.execute("PRAGMA table_info(executions)").fetchall()
        event_rows = connection.execute(
            "PRAGMA table_info(execution_events)"
        ).fetchall()
        manifest_rows = connection.execute(
            "PRAGMA table_info(execution_artifact_manifests)"
        ).fetchall()
        artifact_rows = connection.execute(
            "PRAGMA table_info(execution_artifacts)"
        ).fetchall()
        if not execution_rows:
            raise ExecutionStoreError("execution database schema is missing executions")
        if not event_rows:
            raise ExecutionStoreError(
                "execution database schema is missing execution_events"
            )
        if not manifest_rows or not artifact_rows:
            raise ExecutionStoreError(
                "execution database schema is missing artifact manifest tables"
            )
        if tuple(row[1] for row in execution_rows) != _EXECUTION_COLUMNS:
            raise ExecutionStoreError("execution database executions schema is invalid")
        if tuple(row[1] for row in event_rows) != _EVENT_COLUMNS:
            raise ExecutionStoreError("execution database event schema is invalid")
        if tuple(row[1] for row in manifest_rows) != _ARTIFACT_MANIFEST_COLUMNS:
            raise ExecutionStoreError(
                "execution database artifact manifest schema is invalid"
            )
        if tuple(row[1] for row in artifact_rows) != _ARTIFACT_COLUMNS:
            raise ExecutionStoreError("execution database artifact schema is invalid")


def validate_execution_db_path(path: str | Path, *, workspace_root: Path) -> Path:
    """Validate an operator-controlled SQLite path outside the workspace."""

    candidate = Path(path)
    if not candidate.is_absolute():
        raise ExecutionStoreError("execution database path must be absolute")
    root = workspace_root.resolve(strict=True)
    parent = candidate.parent
    try:
        parent_stat = parent.lstat()
        resolved_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExecutionStoreError(
            f"cannot inspect execution database parent safely: {exc}"
        ) from exc
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise ExecutionStoreError("execution database parent must be a directory")
    resolved = resolved_parent / candidate.name
    if resolved == root or root in resolved.parents:
        raise ExecutionStoreError(
            "execution database must be outside the configured workspace root"
        )
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ExecutionStoreError(
            f"cannot inspect execution database safely: {exc}"
        ) from exc
    else:
        if stat.S_ISLNK(metadata.st_mode):
            raise ExecutionStoreError("execution database must not be a symbolic link")
        if not stat.S_ISREG(metadata.st_mode):
            raise ExecutionStoreError("execution database must be a regular file")
    return resolved


def reconcile_unfinished_executions(store: ExecutionStore) -> list[ExecutionRecord]:
    """Reconcile old-process unfinished history without adopting runtime resources."""

    reconciled: list[ExecutionRecord] = []
    now = time.time()
    for record in store.list_unfinished():
        reconciled.append(
            store.transition(
                record.execution_id,
                {record.state},
                ExecutionState.CRASHED,
                reason=ExecutionReason.SERVER_RESTARTED,
                finished_at=now,
                updated_at=now,
            )
        )
    return reconciled


def _validate_event_page_args(cursor: int, limit: int) -> None:
    if type(cursor) is not int or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= _MAX_EVENT_PAGE_SIZE:
        raise ValueError("limit must be an integer between 1 and 100")


def _created_event(record: ExecutionRecord) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id=record.execution_id,
        sequence=1,
        timestamp=record.created_at,
        event_type=ExecutionEventType.CREATED,
        from_state=None,
        to_state=record.state,
        reason=record.reason,
        error_summary=record.error_summary,
    )


def _transition_event(
    current: ExecutionRecord, updated: ExecutionRecord, sequence: int
) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id=updated.execution_id,
        sequence=sequence,
        timestamp=updated.updated_at,
        event_type=ExecutionEventType.STATE_TRANSITION,
        from_state=current.state,
        to_state=updated.state,
        reason=updated.reason,
        error_summary=updated.error_summary,
    )


def _cancellation_requested_event(
    current: ExecutionRecord,
    reason: ExecutionReason,
    sequence: int,
    timestamp: float,
) -> ExecutionEvent:
    return ExecutionEvent(
        execution_id=current.execution_id,
        sequence=sequence,
        timestamp=timestamp,
        event_type=ExecutionEventType.CANCELLATION_REQUESTED,
        from_state=current.state,
        to_state=current.state,
        reason=reason,
        error_summary=None,
    )


def _validate_event_history(
    record: ExecutionRecord, events: list[ExecutionEvent]
) -> bool:
    if not events:
        return False
    for event in events:
        if event.execution_id != record.execution_id:
            raise ExecutionStoreError(
                "execution database contains an invalid event history"
            )
    first = events[0]
    if first.sequence != 1 or first.event_type is not ExecutionEventType.CREATED:
        if any(event.event_type is ExecutionEventType.CREATED for event in events):
            raise ExecutionStoreError(
                "execution database contains an invalid event history"
            )
        return False
    if first.timestamp != record.created_at:
        raise ExecutionStoreError(
            "execution database contains an invalid event history"
        )

    current_state = ExecutionState.STARTING
    previous_timestamp = first.timestamp
    last_state_timestamp = first.timestamp
    last_state_reason = first.reason
    last_state_error_summary = first.error_summary
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence != expected_sequence or event.timestamp < previous_timestamp:
            raise ExecutionStoreError(
                "execution database contains an invalid event history"
            )
        if expected_sequence == 1:
            previous_timestamp = event.timestamp
            continue
        if event.event_type is ExecutionEventType.CREATED:
            raise ExecutionStoreError(
                "execution database contains an invalid event history"
            )
        if event.event_type is ExecutionEventType.CANCELLATION_REQUESTED:
            if (
                event.from_state is not current_state
                or event.to_state is not current_state
            ):
                raise ExecutionStoreError(
                    "execution database contains an invalid event history"
                )
        else:
            if event.from_state is not current_state:
                raise ExecutionStoreError(
                    "execution database contains an invalid event history"
                )
            try:
                ensure_execution_transition(current_state, event.to_state)
            except ValueError as exc:
                raise ExecutionStoreError(
                    "execution database contains an invalid event history"
                ) from exc
            current_state = event.to_state
            last_state_timestamp = event.timestamp
            last_state_reason = event.reason
            last_state_error_summary = event.error_summary
        previous_timestamp = event.timestamp

    if (
        current_state is not record.state
        or last_state_timestamp != record.updated_at
        or last_state_reason is not record.reason
        or last_state_error_summary != record.error_summary
    ):
        raise ExecutionStoreError(
            "execution database contains an invalid event history"
        )
    return True


def _transitioned_record(
    current: ExecutionRecord,
    new_state: ExecutionState,
    *,
    reason: ExecutionReason | None,
    exit_code: int | None,
    resources: ExecutionResources | None,
    started_at: float | None,
    finished_at: float | None,
    updated_at: float | None,
) -> ExecutionRecord:
    now = time.time() if updated_at is None else updated_at
    actual_started = current.started_at if started_at is None else started_at
    actual_finished = current.finished_at if finished_at is None else finished_at
    if (
        new_state in {ExecutionState.RUNNING, ExecutionState.CANCELLING}
        and actual_started is None
    ):
        actual_started = now
    if (
        new_state
        in {
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.TIMED_OUT,
            ExecutionState.CRASHED,
        }
        and actual_finished is None
    ):
        actual_finished = now
    values = current.model_dump()
    values.update(
        {
            "state": new_state,
            "updated_at": now,
            "started_at": actual_started,
            "finished_at": actual_finished,
            "exit_code": exit_code,
            "reason": reason,
            "error_summary": public_execution_error_summary(new_state, reason),
            "resources": resources,
        }
    )
    return ExecutionRecord.model_validate(values)


def _create_v1_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE executions ("
        "execution_id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, "
        "tool TEXT, mode TEXT NOT NULL, state TEXT NOT NULL, "
        "created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, "
        "finished_at REAL, exit_code INTEGER, "
        "reason TEXT, error_summary TEXT)"
    )
    connection.execute(
        "CREATE INDEX executions_state_created ON executions(state, created_at)"
    )


def _create_event_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE execution_events ("
        "execution_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
        "event_type TEXT NOT NULL, timestamp REAL NOT NULL, from_state TEXT, "
        "to_state TEXT NOT NULL, reason TEXT, "
        "error_summary TEXT, PRIMARY KEY (execution_id, sequence), "
        "FOREIGN KEY (execution_id) REFERENCES executions(execution_id))"
    )


def _create_artifact_manifest_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE execution_artifact_manifests ("
        "execution_id TEXT PRIMARY KEY, complete INTEGER NOT NULL "
        "CHECK (complete IN (0, 1)), "
        "FOREIGN KEY (execution_id) REFERENCES executions(execution_id))"
    )
    connection.execute(
        "CREATE TABLE execution_artifacts ("
        "artifact_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, "
        "name TEXT NOT NULL, media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, "
        "sha256 TEXT NOT NULL, created_at REAL NOT NULL, "
        "FOREIGN KEY (execution_id) REFERENCES executions(execution_id))"
    )
    connection.execute(
        "CREATE INDEX execution_artifacts_execution_id "
        "ON execution_artifacts(execution_id)"
    )
    connection.execute(
        "INSERT INTO execution_artifact_manifests (execution_id, complete) "
        "SELECT execution_id, 0 FROM executions"
    )


def _scrub_legacy_error_summaries(connection: sqlite3.Connection) -> None:
    """Remove caller/runtime-authored diagnostics while upgrading pre-v4 audit data."""

    connection.execute("UPDATE executions SET error_summary = NULL")
    connection.execute("UPDATE execution_events SET error_summary = NULL")


def _add_resource_columns(connection: sqlite3.Connection) -> None:
    for name in (
        "wall_time_ms",
        "cpu_time_ms",
        "peak_memory_bytes",
        "workspace_initial_bytes",
        "workspace_final_bytes",
        "workspace_growth_bytes",
        "stdout_bytes",
        "stderr_bytes",
        "output_bytes",
    ):
        connection.execute(f"ALTER TABLE executions ADD COLUMN {name} INTEGER")


def _normalize_artifact_manifest(
    execution_id: str,
    artifact_manifest: Iterable[ArtifactRecord] | None,
    *,
    terminal: bool,
) -> tuple[ArtifactRecord, ...] | None:
    if artifact_manifest is None:
        return None
    if not terminal:
        raise ValueError(
            "artifact manifest may be persisted only for terminal execution"
        )
    artifacts = tuple(artifact_manifest)
    if len(artifacts) > _MAX_ARTIFACT_MANIFEST_ITEMS:
        raise ValueError(
            f"artifact manifest exceeds {_MAX_ARTIFACT_MANIFEST_ITEMS} items"
        )
    artifact_ids: set[str] = set()
    for artifact in artifacts:
        if artifact.execution_id != execution_id:
            raise ValueError("artifact manifest execution_id mismatch")
        if artifact.artifact_id in artifact_ids:
            raise ValueError("artifact manifest contains duplicate artifact_id")
        artifact_ids.add(artifact.artifact_id)
    return artifacts


def _artifact_values(record: ArtifactRecord) -> tuple[object, ...]:
    return (
        record.artifact_id,
        record.execution_id,
        record.name,
        record.media_type,
        record.size_bytes,
        record.sha256,
        record.created_at,
    )


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    try:
        return ArtifactRecord.model_validate(dict(row))
    except ValidationError as exc:
        raise ExecutionStoreError(
            "execution database contains invalid artifact manifest metadata"
        ) from exc


def _resource_values(resources: ExecutionResources | None) -> tuple[object, ...]:
    if resources is None:
        return (None,) * 9
    return (
        resources.wall_time_ms,
        resources.cpu_time_ms,
        resources.peak_memory_bytes,
        resources.workspace_initial_bytes,
        resources.workspace_final_bytes,
        resources.workspace_growth_bytes,
        resources.stdout_bytes,
        resources.stderr_bytes,
        resources.output_bytes,
    )


def _record_values(record: ExecutionRecord) -> tuple[object, ...]:
    return (
        record.execution_id,
        record.kind.value,
        record.name,
        record.tool,
        record.mode.value,
        record.state.value,
        record.created_at,
        record.updated_at,
        record.started_at,
        record.finished_at,
        record.exit_code,
        record.reason.value if record.reason is not None else None,
        record.error_summary,
        *_resource_values(record.resources),
    )


def _event_values(event: ExecutionEvent) -> tuple[object, ...]:
    return (
        event.execution_id,
        event.sequence,
        event.event_type.value,
        event.timestamp,
        event.from_state.value if event.from_state is not None else None,
        event.to_state.value,
        event.reason.value if event.reason is not None else None,
        event.error_summary,
    )


def _record_from_row(row: sqlite3.Row) -> ExecutionRecord:
    values = dict(row)
    resource_values = {
        name: values.pop(name)
        for name in (
            "wall_time_ms",
            "cpu_time_ms",
            "peak_memory_bytes",
            "workspace_initial_bytes",
            "workspace_final_bytes",
            "workspace_growth_bytes",
            "stdout_bytes",
            "stderr_bytes",
            "output_bytes",
        )
    }
    try:
        if resource_values["wall_time_ms"] is None:
            if any(value is not None for value in resource_values.values()):
                raise ValueError("partial resource accounting record")
            resources = None
        else:
            resources = ExecutionResources.model_validate(resource_values)
        values["resources"] = resources
        return ExecutionRecord.model_validate(values)
    except (ValidationError, ValueError) as exc:
        raise ExecutionStoreError(
            "execution database contains an invalid record"
        ) from exc


def _event_from_row(row: sqlite3.Row) -> ExecutionEvent:
    try:
        return ExecutionEvent.model_validate(dict(row))
    except ValidationError as exc:
        raise ExecutionStoreError(
            "execution database contains an invalid event"
        ) from exc
