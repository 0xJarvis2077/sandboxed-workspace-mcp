from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from workspace_guard_mcp.artifact import ArtifactRecord
from workspace_guard_mcp.execution import (
    ExecutionEventType,
    ExecutionKind,
    ExecutionMode,
    ExecutionReason,
    ExecutionRecord,
    ExecutionResources,
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


def resources() -> ExecutionResources:
    return ExecutionResources(
        wall_time_ms=25,
        cpu_time_ms=None,
        peak_memory_bytes=None,
        workspace_initial_bytes=100,
        workspace_final_bytes=125,
        workspace_growth_bytes=25,
        stdout_bytes=7,
        stderr_bytes=3,
        output_bytes=10,
    )


def artifact(execution_id: str = "exec-test") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id="A" * 32,
        execution_id=execution_id,
        name="coverage.xml",
        media_type="application/xml",
        size_bytes=12,
        sha256="a" * 64,
        created_at=time.time(),
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

    def test_terminal_transition_persists_resources(self) -> None:
        store = InMemoryExecutionStore()
        store.create(record())
        store.transition("exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING)
        completed = store.transition(
            "exec-test",
            {ExecutionState.RUNNING},
            ExecutionState.SUCCEEDED,
            exit_code=0,
            resources=resources(),
        )
        self.assertEqual(completed.resources, resources())
        self.assertEqual(store.get("exec-test").resources, resources())

    def test_starting_cancellation_request_is_audited_before_transition(self) -> None:
        store = InMemoryExecutionStore()
        store.create(record())
        cancelling = store.request_cancellation(
            "exec-test",
            {ExecutionState.STARTING},
            ExecutionReason.CLIENT_CANCELLED,
        )
        self.assertEqual(cancelling.state, ExecutionState.CANCELLING)
        self.assertIsNotNone(cancelling.started_at)
        self.assertIsNone(cancelling.finished_at)
        events = store.list_events("exec-test").events
        self.assertEqual(
            [event.event_type for event in events],
            [
                ExecutionEventType.CREATED,
                ExecutionEventType.CANCELLATION_REQUESTED,
                ExecutionEventType.STATE_TRANSITION,
            ],
        )
        self.assertEqual(events[1].from_state, ExecutionState.STARTING)
        self.assertEqual(events[1].to_state, ExecutionState.STARTING)
        self.assertEqual(events[1].reason, ExecutionReason.CLIENT_CANCELLED)
        self.assertEqual(events[2].from_state, ExecutionState.STARTING)
        self.assertEqual(events[2].to_state, ExecutionState.CANCELLING)

    def test_events_are_atomic_ordered_and_bounded(self) -> None:
        store = InMemoryExecutionStore()
        store.create(record())
        running = store.transition(
            "exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING
        )
        succeeded = store.transition(
            "exec-test",
            {ExecutionState.RUNNING},
            ExecutionState.SUCCEEDED,
            exit_code=0,
        )
        first = store.list_events("exec-test", limit=2)
        self.assertTrue(first.history_complete)
        self.assertTrue(first.has_more)
        self.assertEqual([event.sequence for event in first.events], [1, 2])
        self.assertEqual(first.events[1].timestamp, running.updated_at)
        second = store.list_events("exec-test", cursor=first.next_cursor, limit=2)
        self.assertFalse(second.has_more)
        self.assertEqual(second.events[0].sequence, 3)
        self.assertEqual(second.events[0].timestamp, succeeded.updated_at)

        before = store.list_events("exec-test").events
        with self.assertRaises(ExecutionConflictError):
            store.transition(
                "exec-test", {ExecutionState.RUNNING}, ExecutionState.FAILED
            )
        self.assertEqual(store.list_events("exec-test").events, before)
        with self.assertRaises(DuplicateExecutionError):
            store.create(record())
        self.assertEqual(store.list_events("exec-test").events, before)

        for cursor, limit in ((-1, 1), (True, 1), (0, 0), (0, 101), (0, True)):
            with (
                self.subTest(cursor=cursor, limit=limit),
                self.assertRaises(ValueError),
            ):
                store.list_events("exec-test", cursor=cursor, limit=limit)

    def test_terminal_retention_evicts_whole_oldest_execution_only(self) -> None:
        store = InMemoryExecutionStore(max_retained_terminal_executions=2)
        active = record("active")
        store.create(active)
        store.transition("active", {ExecutionState.STARTING}, ExecutionState.RUNNING)

        for index in range(3):
            execution_id = f"done-{index}"
            store.create(record(execution_id))
            store.transition(
                execution_id,
                {ExecutionState.STARTING},
                ExecutionState.RUNNING,
            )
            store.transition(
                execution_id,
                {ExecutionState.RUNNING},
                ExecutionState.SUCCEEDED,
                exit_code=0,
            )

        with self.assertRaises(UnknownExecutionError):
            store.get("done-0")
        with self.assertRaises(UnknownExecutionError):
            store.list_events("done-0")
        self.assertEqual(store.get("done-1").state, ExecutionState.SUCCEEDED)
        self.assertEqual(store.get("done-2").state, ExecutionState.SUCCEEDED)
        self.assertEqual(store.get("active").state, ExecutionState.RUNNING)
        self.assertEqual(
            [item.execution_id for item in store.list_unfinished()], ["active"]
        )

    def test_terminal_retention_configuration_is_bounded(self) -> None:
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                InMemoryExecutionStore(value)  # type: ignore[arg-type]

    def test_terminal_manifest_is_complete_and_evicted_with_execution(self) -> None:
        store = InMemoryExecutionStore(max_retained_terminal_executions=1)
        for execution_id in ("first", "second"):
            store.create(record(execution_id))
            store.transition(
                execution_id,
                {ExecutionState.STARTING},
                ExecutionState.RUNNING,
            )
            store.transition(
                execution_id,
                {ExecutionState.RUNNING},
                ExecutionState.SUCCEEDED,
                exit_code=0,
                artifact_manifest=[artifact(execution_id)],
            )
        with self.assertRaises(UnknownExecutionError):
            store.list_artifacts("first")
        manifest = store.list_artifacts("second")
        self.assertTrue(manifest.manifest_complete)
        self.assertEqual(len(manifest.artifacts), 1)
        self.assertEqual(manifest.artifacts[0].execution_id, "second")
        self.assertEqual(manifest.artifacts[0].artifact_id, "A" * 32)

    def test_nonterminal_manifest_is_rejected(self) -> None:
        store = InMemoryExecutionStore()
        store.create(record())
        with self.assertRaises(ValueError):
            store.transition(
                "exec-test",
                {ExecutionState.STARTING},
                ExecutionState.RUNNING,
                artifact_manifest=[artifact()],
            )


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
            resources=resources(),
        )
        self.assertEqual(running.state, ExecutionState.RUNNING)
        self.assertEqual(succeeded.state, ExecutionState.SUCCEEDED)
        self.assertEqual(succeeded.resources, resources())

        reopened = SqliteExecutionStore(self.path)
        reopened_record = reopened.get(original.execution_id)
        self.assertEqual(reopened_record.state, ExecutionState.SUCCEEDED)
        self.assertEqual(reopened_record.resources, resources())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
            )

    def test_terminal_artifact_manifest_is_durable_and_atomic_with_record(self) -> None:
        store = SqliteExecutionStore(self.path)
        store.create(record())
        store.transition("exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING)
        admitted = artifact()
        terminal = store.transition(
            "exec-test",
            {ExecutionState.RUNNING},
            ExecutionState.SUCCEEDED,
            exit_code=0,
            resources=resources(),
            artifact_manifest=[admitted],
        )
        reopened = SqliteExecutionStore(self.path)
        manifest = reopened.list_artifacts("exec-test")
        self.assertTrue(manifest.manifest_complete)
        self.assertEqual(manifest.artifacts, [admitted])
        self.assertEqual(reopened.get("exec-test"), terminal)
        with sqlite3.connect(self.path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(execution_artifacts)"
                ).fetchall()
            }
        self.assertNotIn("owner_scope", columns)
        self.assertNotIn("content", columns)
        self.assertNotIn("path", columns)

    def test_artifact_manifest_consistency_corruption_fails_closed(self) -> None:
        rows_path = self.base / "manifest-rows-corrupt.sqlite3"
        rows_store = SqliteExecutionStore(rows_path)
        rows_store.create(record())
        admitted = artifact()
        with sqlite3.connect(rows_path) as connection:
            connection.execute(
                "INSERT INTO execution_artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    admitted.artifact_id,
                    admitted.execution_id,
                    admitted.name,
                    admitted.media_type,
                    admitted.size_bytes,
                    admitted.sha256,
                    admitted.created_at,
                ),
            )
        with self.assertRaises(ExecutionStoreError):
            rows_store.list_artifacts("exec-test")

        state_path = self.base / "manifest-state-corrupt.sqlite3"
        state_store = SqliteExecutionStore(state_path)
        state_store.create(record())
        with sqlite3.connect(state_path) as connection:
            connection.execute(
                "UPDATE execution_artifact_manifests SET complete=1 "
                "WHERE execution_id='exec-test'"
            )
        with self.assertRaises(ExecutionStoreError):
            state_store.list_artifacts("exec-test")

    def test_artifact_manifest_insert_failure_rolls_back_terminal_truth(self) -> None:
        store = SqliteExecutionStore(self.path)
        store.create(record())
        store.transition("exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING)
        before_events = store.list_events("exec-test").events
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_manifest BEFORE INSERT ON execution_artifacts "
                "WHEN NEW.execution_id='exec-test' "
                "BEGIN SELECT RAISE(ABORT, 'forced manifest failure'); END"
            )
        with self.assertRaises(ExecutionStoreError):
            store.transition(
                "exec-test",
                {ExecutionState.RUNNING},
                ExecutionState.SUCCEEDED,
                exit_code=0,
                resources=resources(),
                artifact_manifest=[artifact()],
            )
        current = store.get("exec-test")
        self.assertEqual(current.state, ExecutionState.RUNNING)
        self.assertIsNone(current.resources)
        self.assertEqual(store.list_events("exec-test").events, before_events)
        self.assertFalse(store.list_artifacts("exec-test").manifest_complete)

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
            connection.execute("PRAGMA user_version=4")
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

    def test_invalid_persisted_resource_record_fails_closed(self) -> None:
        store = SqliteExecutionStore(self.path)
        store.create(record())
        store.transition("exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING)
        store.transition(
            "exec-test",
            {ExecutionState.RUNNING},
            ExecutionState.SUCCEEDED,
            exit_code=0,
            resources=resources(),
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE executions SET output_bytes = 999 WHERE execution_id = ?",
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
                updated = store.transition(
                    "exec-test",
                    {ExecutionState.RUNNING},
                    state,
                    exit_code=0 if state is ExecutionState.SUCCEEDED else 2,
                )
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
        events = store.list_events("exec-test").events
        terminal_events = [
            event
            for event in events
            if event.from_state is ExecutionState.RUNNING
            and event.to_state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED}
        ]
        self.assertEqual(len(terminal_events), 1)

    def test_sqlite_events_persist_paginate_and_fail_closed_on_corruption(self) -> None:
        store = SqliteExecutionStore(self.path)
        original = record()
        store.create(original)
        running = store.transition(
            "exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING
        )
        failed = store.transition(
            "exec-test",
            {ExecutionState.RUNNING},
            ExecutionState.FAILED,
            reason=ExecutionReason.OUTPUT_LIMIT_EXCEEDED,
        )
        page = SqliteExecutionStore(self.path).list_events("exec-test", limit=2)
        self.assertTrue(page.history_complete)
        self.assertTrue(page.has_more)
        self.assertEqual([event.sequence for event in page.events], [1, 2])
        self.assertEqual(page.events[1].timestamp, running.updated_at)
        second = store.list_events("exec-test", cursor=page.next_cursor, limit=2)
        self.assertFalse(second.has_more)
        self.assertEqual(second.next_cursor, 3)
        self.assertEqual(second.events[0].timestamp, failed.updated_at)
        self.assertEqual(second.events[0].reason, ExecutionReason.OUTPUT_LIMIT_EXCEEDED)
        self.assertIsNone(second.events[0].error_summary)

        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE execution_events SET event_type='whatever' "
                "WHERE execution_id=? AND sequence=2",
                ("exec-test",),
            )
        with self.assertRaises(ExecutionStoreError):
            store.list_events("exec-test")

    def test_sqlite_detects_modern_event_history_gaps_and_mismatches(self) -> None:
        corruptions = (
            "DELETE FROM execution_events WHERE execution_id='exec-test' "
            "AND sequence=2",
            "UPDATE execution_events SET timestamp=0 WHERE execution_id='exec-test' "
            "AND sequence=3",
            "DELETE FROM execution_events WHERE execution_id='exec-test' "
            "AND sequence=3",
            "UPDATE execution_events SET to_state='running' "
            "WHERE execution_id='exec-test' AND sequence=1",
            "UPDATE execution_events SET reason='timeout' "
            "WHERE execution_id='exec-test' AND sequence=3",
            "UPDATE execution_events SET error_summary='SECRET' "
            "WHERE execution_id='exec-test' AND sequence=3",
        )
        for index, statement in enumerate(corruptions):
            with self.subTest(index=index):
                path = self.base / f"corrupt-{index}.sqlite3"
                store = SqliteExecutionStore(path)
                store.create(record())
                store.transition(
                    "exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING
                )
                store.transition(
                    "exec-test",
                    {ExecutionState.RUNNING},
                    ExecutionState.SUCCEEDED,
                    exit_code=0,
                )
                with sqlite3.connect(path) as connection:
                    connection.execute(statement)
                with self.assertRaises(ExecutionStoreError):
                    store.list_events("exec-test", limit=1)

    def test_sqlite_detects_semantically_valid_final_event_metadata_mismatch(
        self,
    ) -> None:
        store = SqliteExecutionStore(self.path)
        store.create(record())
        store.transition("exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING)
        store.transition(
            "exec-test",
            {ExecutionState.RUNNING},
            ExecutionState.FAILED,
            reason=ExecutionReason.OUTPUT_LIMIT_EXCEEDED,
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE execution_events SET reason='workspace_limit_exceeded' "
                "WHERE execution_id='exec-test' AND sequence=3"
            )
        with self.assertRaises(ExecutionStoreError):
            store.list_events("exec-test")

    def test_cancellation_request_is_atomic_persistent_audit_fact(self) -> None:
        store = SqliteExecutionStore(self.path)
        store.create(record())
        store.transition("exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING)
        cancelling = store.request_cancellation(
            "exec-test", {ExecutionState.RUNNING}, ExecutionReason.CLIENT_CANCELLED
        )
        self.assertEqual(cancelling.state, ExecutionState.CANCELLING)
        page = SqliteExecutionStore(self.path).list_events("exec-test")
        self.assertTrue(page.history_complete)
        self.assertEqual(
            [event.event_type.value for event in page.events],
            [
                "created",
                "state_transition",
                "cancellation_requested",
                "state_transition",
            ],
        )
        requested = page.events[2]
        self.assertEqual(requested.from_state, ExecutionState.RUNNING)
        self.assertEqual(requested.to_state, ExecutionState.RUNNING)
        self.assertEqual(requested.reason, ExecutionReason.CLIENT_CANCELLED)

    def test_cancellation_request_event_failure_rolls_back_state(self) -> None:
        store = SqliteExecutionStore(self.path)
        store.create(record())
        store.transition("exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING)
        before = store.list_events("exec-test").events
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_cancel_request BEFORE INSERT "
                "ON execution_events WHEN NEW.execution_id='exec-test' "
                "AND NEW.event_type='cancellation_requested' "
                "BEGIN SELECT RAISE(ABORT, 'forced cancellation audit failure'); END"
            )
        with self.assertRaises(ExecutionStoreError):
            store.request_cancellation(
                "exec-test",
                {ExecutionState.RUNNING},
                ExecutionReason.USER_CANCELLED,
            )
        self.assertEqual(store.get("exec-test").state, ExecutionState.RUNNING)
        self.assertEqual(store.list_events("exec-test").events, before)

    def test_sqlite_event_insert_failures_roll_back_record_changes(self) -> None:
        store = SqliteExecutionStore(self.path)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_created BEFORE INSERT ON execution_events "
                "WHEN NEW.execution_id='create-fails' "
                "BEGIN SELECT RAISE(ABORT, 'forced event failure'); END"
            )
        with self.assertRaises(ExecutionStoreError):
            store.create(record("create-fails"))
        with self.assertRaises(UnknownExecutionError):
            store.get("create-fails")

        store.create(record())
        before = store.list_events("exec-test").events
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_transition BEFORE INSERT ON execution_events "
                "WHEN NEW.execution_id='exec-test' AND NEW.sequence > 1 "
                "BEGIN SELECT RAISE(ABORT, 'forced event failure'); END"
            )
        with self.assertRaises(ExecutionStoreError):
            store.transition(
                "exec-test", {ExecutionState.STARTING}, ExecutionState.RUNNING
            )
        self.assertEqual(store.get("exec-test").state, ExecutionState.STARTING)
        self.assertEqual(store.list_events("exec-test").events, before)

    def test_v1_migration_preserves_record_without_fabricating_history(self) -> None:
        old = record("legacy")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE executions ("
                "execution_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "name TEXT NOT NULL, tool TEXT, mode TEXT NOT NULL, "
                "state TEXT NOT NULL, created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL, "
                "started_at REAL, finished_at REAL, exit_code INTEGER, "
                "reason TEXT, error_summary TEXT)"
            )
            connection.execute(
                "CREATE INDEX executions_state_created ON executions(state, created_at)"
            )
            connection.execute(
                "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    old.execution_id,
                    old.kind.value,
                    old.name,
                    old.tool,
                    old.mode.value,
                    old.state.value,
                    old.created_at,
                    old.updated_at,
                    old.started_at,
                    old.finished_at,
                    old.exit_code,
                    None,
                    old.error_summary,
                ),
            )
            connection.execute("PRAGMA user_version=1")
        store = SqliteExecutionStore(self.path)
        self.assertEqual(store.get("legacy"), old)
        page = store.list_events("legacy")
        self.assertEqual(page.events, [])
        self.assertFalse(page.history_complete)
        reconciled = reconcile_unfinished_executions(store)
        self.assertEqual(reconciled[0].state, ExecutionState.CRASHED)
        page = store.list_events("legacy")
        self.assertEqual(len(page.events), 1)
        self.assertEqual(page.events[0].sequence, 1)
        self.assertEqual(page.events[0].from_state, ExecutionState.STARTING)
        self.assertFalse(page.history_complete)
        self.assertIsNone(store.get("legacy").resources)
        self.assertFalse(store.list_artifacts("legacy").manifest_complete)

    def test_v2_migration_preserves_records_events_and_missing_resources(self) -> None:
        old = record("legacy-v2")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE executions ("
                "execution_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "name TEXT NOT NULL, tool TEXT, mode TEXT NOT NULL, "
                "state TEXT NOT NULL, created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL, started_at REAL, finished_at REAL, "
                "exit_code INTEGER, reason TEXT, error_summary TEXT)"
            )
            connection.execute(
                "CREATE INDEX executions_state_created ON executions(state, created_at)"
            )
            connection.execute(
                "CREATE TABLE execution_events ("
                "execution_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
                "event_type TEXT NOT NULL, timestamp REAL NOT NULL, from_state TEXT, "
                "to_state TEXT NOT NULL, reason TEXT, error_summary TEXT, "
                "PRIMARY KEY (execution_id, sequence), "
                "FOREIGN KEY (execution_id) REFERENCES executions(execution_id))"
            )
            connection.execute(
                "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    old.execution_id,
                    old.kind.value,
                    old.name,
                    old.tool,
                    old.mode.value,
                    old.state.value,
                    old.created_at,
                    old.updated_at,
                    old.started_at,
                    old.finished_at,
                    old.exit_code,
                    None,
                    "TOPSECRET legacy runtime detail",
                ),
            )
            connection.execute(
                "INSERT INTO execution_events VALUES "
                "(?, 1, 'created', ?, NULL, ?, NULL, ?)",
                (
                    old.execution_id,
                    old.created_at,
                    old.state.value,
                    "TOPSECRET legacy event detail",
                ),
            )
            connection.execute("PRAGMA user_version=2")

        migrated = SqliteExecutionStore(self.path)
        migrated_record = migrated.get("legacy-v2")
        self.assertEqual(migrated_record, old)
        self.assertIsNone(migrated_record.resources)
        migrated_events = migrated.list_events("legacy-v2").events
        self.assertEqual(len(migrated_events), 1)
        self.assertIsNone(migrated_events[0].error_summary)
        self.assertFalse(migrated.list_artifacts("legacy-v2").manifest_complete)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_v3_migration_preserves_resource_accounting_and_marks_manifest_unknown(
        self,
    ) -> None:
        expected_resources = resources()
        expected = ExecutionRecord(
            execution_id="legacy-v3",
            kind=ExecutionKind.TASK,
            name="test",
            tool="run_task",
            mode=ExecutionMode.RUN,
            state=ExecutionState.SUCCEEDED,
            created_at=10.0,
            updated_at=12.0,
            started_at=11.0,
            finished_at=12.0,
            exit_code=0,
            resources=expected_resources,
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE executions ("
                "execution_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "name TEXT NOT NULL, tool TEXT, mode TEXT NOT NULL, "
                "state TEXT NOT NULL, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, "
                "finished_at REAL, exit_code INTEGER, reason TEXT, error_summary TEXT, "
                "wall_time_ms INTEGER, cpu_time_ms INTEGER, peak_memory_bytes INTEGER, "
                "workspace_initial_bytes INTEGER, workspace_final_bytes INTEGER, "
                "workspace_growth_bytes INTEGER, stdout_bytes INTEGER, "
                "stderr_bytes INTEGER, output_bytes INTEGER)"
            )
            connection.execute(
                "CREATE INDEX executions_state_created ON executions(state, created_at)"
            )
            connection.execute(
                "CREATE TABLE execution_events ("
                "execution_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
                "event_type TEXT NOT NULL, timestamp REAL NOT NULL, from_state TEXT, "
                "to_state TEXT NOT NULL, reason TEXT, error_summary TEXT, "
                "PRIMARY KEY (execution_id, sequence), "
                "FOREIGN KEY (execution_id) REFERENCES executions(execution_id))"
            )
            connection.execute(
                "INSERT INTO executions VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    expected.execution_id,
                    expected.kind.value,
                    expected.name,
                    expected.tool,
                    expected.mode.value,
                    expected.state.value,
                    expected.created_at,
                    expected.updated_at,
                    expected.started_at,
                    expected.finished_at,
                    expected.exit_code,
                    None,
                    None,
                    expected_resources.wall_time_ms,
                    expected_resources.cpu_time_ms,
                    expected_resources.peak_memory_bytes,
                    expected_resources.workspace_initial_bytes,
                    expected_resources.workspace_final_bytes,
                    expected_resources.workspace_growth_bytes,
                    expected_resources.stdout_bytes,
                    expected_resources.stderr_bytes,
                    expected_resources.output_bytes,
                ),
            )
            connection.executemany(
                "INSERT INTO execution_events VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                [
                    ("legacy-v3", 1, "created", 10.0, None, "starting"),
                    ("legacy-v3", 2, "state_transition", 11.0, "starting", "running"),
                    ("legacy-v3", 3, "state_transition", 12.0, "running", "succeeded"),
                ],
            )
            connection.execute("PRAGMA user_version=3")

        migrated = SqliteExecutionStore(self.path)
        self.assertEqual(migrated.get("legacy-v3"), expected)
        self.assertTrue(migrated.list_events("legacy-v3").history_complete)
        manifest = migrated.list_artifacts("legacy-v3")
        self.assertFalse(manifest.manifest_complete)
        self.assertEqual(manifest.artifacts, [])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)

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
                    exit_code=0 if state is ExecutionState.SUCCEEDED else 2,
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
