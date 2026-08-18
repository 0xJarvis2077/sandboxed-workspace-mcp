from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from workspace_guard_mcp.execution import (
    ExecutionKind,
    ExecutionMode,
    ExecutionReason,
    ExecutionRecord,
    ExecutionState,
)
from workspace_guard_mcp.execution_store import (
    DuplicateExecutionError,
    ExecutionConflictError,
    ExecutionStoreError,
    InMemoryExecutionStore,
    SqliteExecutionStore,
    UnknownExecutionError,
    reconcile_unfinished_executions,
    validate_execution_db_path,
)


def record(
    execution_id: str = "exec-test",
    *,
    state: ExecutionState = ExecutionState.STARTING,
) -> ExecutionRecord:
    now = time.time()
    started_at = now if state is not ExecutionState.STARTING else None
    finished_at = (
        now
        if state
        in {
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.TIMED_OUT,
            ExecutionState.CRASHED,
        }
        else None
    )
    return ExecutionRecord(
        execution_id=execution_id,
        kind=ExecutionKind.TASK,
        name="test",
        tool="run_task",
        mode=ExecutionMode.RUN,
        state=state,
        created_at=now,
        updated_at=now,
        started_at=started_at,
        finished_at=finished_at,
    )


class InMemoryExecutionStoreTests(unittest.TestCase):
    def test_create_get_duplicate_transition_cas_and_unknown(self) -> None:
        store = InMemoryExecutionStore()
        original = record()
        store.create(original)
        self.assertEqual(store.get(original.execution_id), original)
        with self.assertRaises(DuplicateExecutionError):
            store.create(original)

        running = store.transition(
            original.execution_id,
            {ExecutionState.STARTING},
            ExecutionState.RUNNING,
        )
        self.assertEqual(running.state, ExecutionState.RUNNING)
        self.assertIsNotNone(running.started_at)
        with self.assertRaises(ExecutionConflictError):
            store.transition(
                original.execution_id,
                {ExecutionState.STARTING},
                ExecutionState.CRASHED,
            )
        with self.assertRaises(ValueError):
            store.transition(
                original.execution_id,
                {ExecutionState.RUNNING},
                ExecutionState.STARTING,
            )
        with self.assertRaises(UnknownExecutionError):
            store.get("missing")

    def test_starting_to_cancelling_initializes_started_at(self) -> None:
        store = InMemoryExecutionStore()
        store.create(record())
        cancelling = store.transition(
            "exec-test",
            {ExecutionState.STARTING},
            ExecutionState.CANCELLING,
            reason=ExecutionReason.USER_CANCELLED,
        )
        self.assertEqual(cancelling.state, ExecutionState.CANCELLING)
        self.assertIsNotNone(cancelling.started_at)
        self.assertIsNone(cancelling.finished_at)


class SqliteExecutionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.path = self.base / "executions.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_get_transition_reopen_wal_and_schema_version(self) -> None:
        store = SqliteExecutionStore(self.path)
        original = record()
        store.create(original)
        running = store.transition(
            original.execution_id,
            {ExecutionState.STARTING},
            ExecutionState.RUNNING,
        )
        succeeded = store.transition(
            original.execution_id,
            {ExecutionState.RUNNING},
            ExecutionState.SUCCEEDED,
            exit_code=0,
        )
        self.assertEqual(running.state, ExecutionState.RUNNING)
        self.assertEqual(succeeded.state, ExecutionState.SUCCEEDED)

        reopened = SqliteExecutionStore(self.path)
        self.assertEqual(
            reopened.get(original.execution_id).state,
            ExecutionState.SUCCEEDED,
        )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
            )

    def test_duplicate_and_unknown_are_fail_closed(self) -> None:
        store = SqliteExecutionStore(self.path)
        original = record()
        store.create(original)
        with self.assertRaises(DuplicateExecutionError):
            store.create(original)
        with self.assertRaises(UnknownExecutionError):
            store.get("missing")

    def test_newer_schema_and_invalid_persisted_record_fail_closed(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version=2")
        with self.assertRaises(ExecutionStoreError):
            SqliteExecutionStore(self.path)

        self.path.unlink()
        store = SqliteExecutionStore(self.path)
        store.create(record())
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE executions SET state = 'not-a-state' WHERE execution_id = ?",
                ("exec-test",),
            )
        with self.assertRaises(ExecutionStoreError):
            store.get("exec-test")

    def test_invalid_persisted_lifecycle_record_fails_closed(self) -> None:
        store = SqliteExecutionStore(self.path)
        store.create(record())
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE executions SET state = 'succeeded', finished_at = NULL "
                "WHERE execution_id = ?",
                ("exec-test",),
            )
        with self.assertRaises(ExecutionStoreError):
            store.get("exec-test")

    def test_concurrent_terminal_transitions_do_not_last_write_win(self) -> None:
        store = SqliteExecutionStore(self.path)
        store.create(record())
        store.transition("exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING)
        barrier = threading.Barrier(2)
        results: list[ExecutionState] = []
        errors: list[BaseException] = []

        def finish(state: ExecutionState) -> None:
            try:
                barrier.wait(timeout=2)
                updated = store.transition("exec-test", {ExecutionState.RUNNING}, state)
                results.append(updated.state)
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=finish, args=(ExecutionState.SUCCEEDED,))
        second = threading.Thread(target=finish, args=(ExecutionState.FAILED,))
        first.start()
        second.start()
        first.join(timeout=3)
        second.join(timeout=3)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ExecutionConflictError)
        self.assertEqual(store.get("exec-test").state, results[0])

    def test_restart_reconciliation_crashes_only_unfinished_records(self) -> None:
        store = SqliteExecutionStore(self.path)
        for state in (
            ExecutionState.STARTING,
            ExecutionState.RUNNING,
            ExecutionState.CANCELLING,
        ):
            item = record(f"unfinished-{state.value}")
            store.create(item)
            if state is ExecutionState.RUNNING:
                store.transition(
                    item.execution_id,
                    {ExecutionState.STARTING},
                    ExecutionState.RUNNING,
                )
            elif state is ExecutionState.CANCELLING:
                store.transition(
                    item.execution_id,
                    {ExecutionState.STARTING},
                    ExecutionState.CANCELLING,
                    reason=ExecutionReason.USER_CANCELLED,
                )
        terminal_ids: list[str] = []
        for state in (
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.TIMED_OUT,
            ExecutionState.CRASHED,
        ):
            item = record(f"terminal-{state.value}")
            store.create(item)
            if state is ExecutionState.CANCELLED:
                store.transition(
                    item.execution_id,
                    {ExecutionState.STARTING},
                    ExecutionState.CANCELLING,
                    reason=ExecutionReason.USER_CANCELLED,
                )
                store.transition(
                    item.execution_id,
                    {ExecutionState.CANCELLING},
                    state,
                    reason=ExecutionReason.USER_CANCELLED,
                )
            elif state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED}:
                store.transition(
                    item.execution_id,
                    {ExecutionState.STARTING},
                    ExecutionState.RUNNING,
                )
                store.transition(
                    item.execution_id,
                    {ExecutionState.RUNNING},
                    state,
                )
            else:
                store.transition(
                    item.execution_id,
                    {ExecutionState.STARTING},
                    state,
                    reason=(
                        ExecutionReason.TIMEOUT
                        if state is ExecutionState.TIMED_OUT
                        else ExecutionReason.RUNTIME_START_FAILED
                    ),
                )
            terminal_ids.append(item.execution_id)

        reconciled = reconcile_unfinished_executions(store)
        self.assertEqual(len(reconciled), 3)
        for item in reconciled:
            self.assertEqual(item.state, ExecutionState.CRASHED)
            self.assertEqual(item.reason, ExecutionReason.SERVER_RESTARTED)
        for execution_id in terminal_ids:
            self.assertTrue(store.get(execution_id).terminal)
            self.assertNotEqual(
                store.get(execution_id).reason,
                ExecutionReason.SERVER_RESTARTED,
            )

    def test_path_policy_rejects_relative_workspace_and_symlink(self) -> None:
        workspace = self.base / "workspace"
        workspace.mkdir()
        with self.assertRaises(ExecutionStoreError):
            validate_execution_db_path(
                Path("relative.sqlite3"), workspace_root=workspace
            )
        with self.assertRaises(ExecutionStoreError):
            validate_execution_db_path(
                workspace / "executions.sqlite3", workspace_root=workspace
            )

        target = self.base / "target.sqlite3"
        target.touch()
        link = self.base / "link.sqlite3"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(ExecutionStoreError):
            validate_execution_db_path(link, workspace_root=workspace)

        safe = self.base / "safe.sqlite3"
        self.assertEqual(
            validate_execution_db_path(safe, workspace_root=workspace),
            safe.resolve(strict=False),
        )


if __name__ == "__main__":
    unittest.main()
