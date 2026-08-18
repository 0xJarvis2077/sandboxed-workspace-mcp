"""Small execution-record stores for in-memory and SQLite persistence."""

from __future__ import annotations

import sqlite3
import stat
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .execution import (
    ExecutionReason,
    ExecutionRecord,
    ExecutionState,
    ensure_execution_transition,
)

_SCHEMA_VERSION = 1
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
)


class ExecutionStoreError(RuntimeError):
    """Base class for fail-closed execution persistence errors."""


class DuplicateExecutionError(ExecutionStoreError):
    """Raised when a manager-issued execution ID already exists."""


class UnknownExecutionError(ExecutionStoreError):
    """Raised when an execution ID is not present in the store."""


class ExecutionConflictError(ExecutionStoreError):
    """Raised when optimistic state checking rejects a transition."""


class ExecutionStore(Protocol):
    """Minimal persistence boundary for canonical execution records."""

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
        error_summary: str | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        updated_at: float | None = None,
    ) -> ExecutionRecord: ...

    def list_unfinished(self) -> list[ExecutionRecord]: ...


class InMemoryExecutionStore:
    """Thread-safe process-local execution history used by default."""

    def __init__(self) -> None:
        self._records: dict[str, ExecutionRecord] = {}
        self._lock = threading.RLock()

    def create(self, record: ExecutionRecord) -> None:
        with self._lock:
            if record.execution_id in self._records:
                raise DuplicateExecutionError(
                    f"duplicate execution_id: {record.execution_id}"
                )
            self._records[record.execution_id] = record

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
        error_summary: str | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        updated_at: float | None = None,
    ) -> ExecutionRecord:
        expected = frozenset(expected_states)
        if not expected:
            raise ValueError("expected_states must not be empty")
        with self._lock:
            current = self.get(execution_id)
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
                error_summary=error_summary,
                started_at=started_at,
                finished_at=finished_at,
                updated_at=updated_at,
            )
            self._records[execution_id] = record
            return record

    def list_unfinished(self) -> list[ExecutionRecord]:
        with self._lock:
            return [
                record
                for record in self._records.values()
                if record.state in _UNFINISHED_STATES
            ]


class SqliteExecutionStore:
    """SQLite-backed execution metadata with short-lived connections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialize()

    def create(self, record: ExecutionRecord) -> None:
        values = _record_values(record)
        placeholders = ", ".join("?" for _ in _EXECUTION_COLUMNS)
        sql = (
            f"INSERT INTO executions ({', '.join(_EXECUTION_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )
        try:
            with self._connect() as connection:
                connection.execute(sql, values)
        except sqlite3.IntegrityError as exc:
            raise DuplicateExecutionError(
                f"duplicate execution_id: {record.execution_id}"
            ) from exc
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
        error_summary: str | None = None,
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
                    error_summary=error_summary,
                    started_at=started_at,
                    finished_at=finished_at,
                    updated_at=updated_at,
                )
                placeholders = ", ".join("?" for _ in expected_values)
                cursor = connection.execute(
                    "UPDATE executions SET "
                    "state = ?, updated_at = ?, started_at = ?, finished_at = ?, "
                    "exit_code = ?, reason = ?, error_summary = ? "
                    f"WHERE execution_id = ? AND state IN ({placeholders})",
                    (
                        updated.state.value,
                        updated.updated_at,
                        updated.started_at,
                        updated.finished_at,
                        updated.exit_code,
                        updated.reason.value if updated.reason is not None else None,
                        updated.error_summary,
                        execution_id,
                        *expected_values,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ExecutionConflictError(
                        f"execution {execution_id} changed during transition"
                    )
                connection.commit()
                return updated
        except (UnknownExecutionError, ExecutionConflictError):
            raise
        except sqlite3.DatabaseError as exc:
            raise ExecutionStoreError(
                f"cannot transition execution record: {exc}"
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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
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
                    connection.executescript(
                        """
                        CREATE TABLE executions (
                            execution_id TEXT PRIMARY KEY,
                            kind TEXT NOT NULL,
                            name TEXT NOT NULL,
                            tool TEXT,
                            mode TEXT NOT NULL,
                            state TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            started_at REAL,
                            finished_at REAL,
                            exit_code INTEGER,
                            reason TEXT,
                            error_summary TEXT
                        );
                        CREATE INDEX executions_state_created
                        ON executions(state, created_at);
                        PRAGMA user_version=1;
                        """
                    )
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
        rows = connection.execute("PRAGMA table_info(executions)").fetchall()
        if not rows:
            raise ExecutionStoreError("execution database schema is missing executions")
        names = tuple(row[1] for row in rows)
        if names != _EXECUTION_COLUMNS:
            raise ExecutionStoreError(
                "execution database schema does not match version 1"
            )


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
    """Mark old-process unfinished history as crashed without adopting containers."""

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


def _transitioned_record(
    current: ExecutionRecord,
    new_state: ExecutionState,
    *,
    reason: ExecutionReason | None,
    exit_code: int | None,
    error_summary: str | None,
    started_at: float | None,
    finished_at: float | None,
    updated_at: float | None,
) -> ExecutionRecord:
    now = time.time() if updated_at is None else updated_at
    actual_started = current.started_at if started_at is None else started_at
    actual_finished = current.finished_at if finished_at is None else finished_at
    if new_state is ExecutionState.RUNNING and actual_started is None:
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
            "error_summary": error_summary,
        }
    )
    return ExecutionRecord.model_validate(values)


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
    )


def _record_from_row(row: sqlite3.Row) -> ExecutionRecord:
    try:
        return ExecutionRecord.model_validate(dict(row))
    except ValidationError as exc:
        raise ExecutionStoreError(
            "execution database contains an invalid record"
        ) from exc
