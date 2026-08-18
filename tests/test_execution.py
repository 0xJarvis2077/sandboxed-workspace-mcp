from __future__ import annotations

import unittest

from pydantic import ValidationError

from workspace_guard_mcp.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionKind,
    ExecutionMode,
    ExecutionReason,
    ExecutionRecord,
    ExecutionResources,
    ExecutionState,
    ExecutionTransitionError,
    ensure_execution_transition,
    is_terminal_state,
    legacy_execution_status,
)


class ExecutionDomainTests(unittest.TestCase):
    def record(self, **updates: object) -> ExecutionRecord:
        values: dict[str, object] = {
            "execution_id": "exec-test",
            "kind": ExecutionKind.TASK,
            "name": "test",
            "tool": "run_task",
            "mode": ExecutionMode.RUN,
            "state": ExecutionState.STARTING,
            "created_at": 1.0,
            "updated_at": 1.0,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "reason": None,
            "error_summary": None,
        }
        values.update(updates)
        return ExecutionRecord.model_validate(values)

    def resources(self, **updates: object) -> ExecutionResources:
        values: dict[str, object] = {
            "wall_time_ms": 100,
            "cpu_time_ms": None,
            "peak_memory_bytes": None,
            "workspace_initial_bytes": 1000,
            "workspace_final_bytes": 1010,
            "workspace_growth_bytes": 10,
            "stdout_bytes": 7,
            "stderr_bytes": 3,
            "output_bytes": 10,
        }
        values.update(updates)
        return ExecutionResources.model_validate(values)

    def test_execution_resources_validate_accounting_invariants(self) -> None:
        all_known = self.resources(cpu_time_ms=20, peak_memory_bytes=4096)
        self.assertEqual(all_known.output_bytes, 10)
        unavailable = self.resources(
            workspace_final_bytes=None,
            workspace_growth_bytes=None,
        )
        self.assertIsNone(unavailable.workspace_final_bytes)
        zero = self.resources(
            workspace_final_bytes=1000,
            workspace_growth_bytes=0,
            stdout_bytes=0,
            stderr_bytes=0,
            output_bytes=0,
        )
        self.assertEqual(zero.workspace_growth_bytes, 0)

        invalid_updates = (
            {"wall_time_ms": -1},
            {"stdout_bytes": -1},
            {"wall_time_ms": True},
            {"output_bytes": 11},
            {"workspace_growth_bytes": 9},
            {"workspace_final_bytes": None, "workspace_growth_bytes": 0},
            {"workspace_initial_bytes": None},
            {"unexpected": 1},
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                self.resources(**updates)

    def test_execution_record_resources_follow_lifecycle(self) -> None:
        resources = self.resources()
        with self.assertRaises(ValidationError):
            self.record(resources=resources)
        terminal = self.record(
            state=ExecutionState.SUCCEEDED,
            updated_at=2.0,
            finished_at=2.0,
            resources=resources,
        )
        self.assertEqual(terminal.resources, resources)
        legacy = self.record(
            state=ExecutionState.SUCCEEDED,
            updated_at=2.0,
            finished_at=2.0,
        )
        self.assertIsNone(legacy.resources)

    def test_valid_record_is_frozen_and_rejects_extra_fields(self) -> None:
        record = self.record()
        self.assertEqual(record.execution_id, "exec-test")
        with self.assertRaises(ValidationError):
            record.state = ExecutionState.RUNNING  # type: ignore[misc]
        values = record.model_dump()
        values["argv"] = ["sh"]
        with self.assertRaises(ValidationError):
            ExecutionRecord.model_validate(values)

    def test_invalid_enum_and_unbounded_strings_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.record(state="unknown")
        with self.assertRaises(ValidationError):
            self.record(error_summary="x" * 4097)

    def test_execution_events_enforce_canonical_history_shape(self) -> None:
        created = ExecutionEvent(
            execution_id="exec-test",
            sequence=1,
            timestamp=1.0,
            event_type=ExecutionEventType.CREATED,
            from_state=None,
            to_state=ExecutionState.STARTING,
            reason=None,
            error_summary=None,
        )
        self.assertEqual(created.sequence, 1)
        transition = ExecutionEvent(
            execution_id="exec-test",
            sequence=2,
            timestamp=2.0,
            event_type=ExecutionEventType.STATE_TRANSITION,
            from_state=ExecutionState.STARTING,
            to_state=ExecutionState.RUNNING,
            reason=None,
            error_summary=None,
        )
        self.assertEqual(transition.to_state, ExecutionState.RUNNING)

        invalid_updates = (
            {
                "event_type": ExecutionEventType.CREATED,
                "from_state": ExecutionState.STARTING,
            },
            {"event_type": ExecutionEventType.STATE_TRANSITION, "from_state": None},
            {
                "event_type": ExecutionEventType.STATE_TRANSITION,
                "from_state": ExecutionState.STARTING,
                "to_state": ExecutionState.SUCCEEDED,
            },
            {"sequence": 0},
            {"sequence": True},
            {"timestamp": float("nan")},
            {"timestamp": float("inf")},
            {"execution_id": "界" * 171},
            {"error_summary": "x" * 4097},
            {"argv": ["sh"]},
        )
        base: dict[str, object] = {
            "execution_id": "exec-test",
            "sequence": 2,
            "timestamp": 2.0,
            "event_type": ExecutionEventType.STATE_TRANSITION,
            "from_state": ExecutionState.STARTING,
            "to_state": ExecutionState.RUNNING,
            "reason": None,
            "error_summary": None,
        }
        for updates in invalid_updates:
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                ExecutionEvent.model_validate({**base, **updates})

    def test_terminal_states_are_exact(self) -> None:
        terminal = {
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.TIMED_OUT,
            ExecutionState.CRASHED,
        }
        for state in ExecutionState:
            with self.subTest(state=state):
                self.assertEqual(is_terminal_state(state), state in terminal)

    def test_transition_table_accepts_only_canonical_edges(self) -> None:
        valid = {
            (ExecutionState.STARTING, ExecutionState.RUNNING),
            (ExecutionState.STARTING, ExecutionState.CANCELLING),
            (ExecutionState.STARTING, ExecutionState.TIMED_OUT),
            (ExecutionState.STARTING, ExecutionState.CRASHED),
            (ExecutionState.RUNNING, ExecutionState.SUCCEEDED),
            (ExecutionState.RUNNING, ExecutionState.FAILED),
            (ExecutionState.RUNNING, ExecutionState.CANCELLING),
            (ExecutionState.RUNNING, ExecutionState.TIMED_OUT),
            (ExecutionState.RUNNING, ExecutionState.CRASHED),
            (ExecutionState.CANCELLING, ExecutionState.CANCELLED),
            (ExecutionState.CANCELLING, ExecutionState.TIMED_OUT),
            (ExecutionState.CANCELLING, ExecutionState.CRASHED),
        }
        for old_state, new_state in valid:
            with self.subTest(old_state=old_state, new_state=new_state):
                ensure_execution_transition(old_state, new_state)

        with self.assertRaises(ExecutionTransitionError):
            ensure_execution_transition(
                ExecutionState.STARTING, ExecutionState.SUCCEEDED
            )
        for terminal in (
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.TIMED_OUT,
            ExecutionState.CRASHED,
        ):
            with (
                self.subTest(terminal=terminal),
                self.assertRaises(ExecutionTransitionError),
            ):
                ensure_execution_transition(terminal, ExecutionState.RUNNING)

    def test_lifecycle_chronology_is_consistent(self) -> None:
        with self.assertRaises(ValidationError):
            self.record(created_at=10.0, updated_at=9.0)
        with self.assertRaises(ValidationError):
            self.record(created_at=10.0, updated_at=10.0, started_at=9.0)
        with self.assertRaises(ValidationError):
            self.record(
                state=ExecutionState.SUCCEEDED,
                created_at=10.0,
                updated_at=11.0,
                started_at=11.0,
                finished_at=10.5,
            )
        crashed = self.record(
            state=ExecutionState.CRASHED,
            created_at=10.0,
            updated_at=11.0,
            finished_at=11.0,
            reason=ExecutionReason.RUNTIME_START_FAILED,
        )
        self.assertIsNone(crashed.started_at)

    def test_state_and_timestamp_consistency(self) -> None:
        for state in (ExecutionState.RUNNING, ExecutionState.CANCELLING):
            with self.subTest(state=state), self.assertRaises(ValidationError):
                self.record(state=state)

        terminal_reasons = {
            ExecutionState.SUCCEEDED: None,
            ExecutionState.FAILED: None,
            ExecutionState.CANCELLED: ExecutionReason.USER_CANCELLED,
            ExecutionState.TIMED_OUT: ExecutionReason.TIMEOUT,
            ExecutionState.CRASHED: ExecutionReason.RUNTIME_START_FAILED,
        }
        for state, reason in terminal_reasons.items():
            with self.subTest(state=state), self.assertRaises(ValidationError):
                self.record(state=state, started_at=1.0, reason=reason)

        for state in (
            ExecutionState.STARTING,
            ExecutionState.RUNNING,
            ExecutionState.CANCELLING,
        ):
            updates: dict[str, object] = {
                "state": state,
                "updated_at": 2.0,
                "finished_at": 2.0,
            }
            if state is not ExecutionState.STARTING:
                updates["started_at"] = 1.0
            with self.subTest(state=state), self.assertRaises(ValidationError):
                self.record(**updates)

    def test_terminal_reason_invariants(self) -> None:
        self.record(
            state=ExecutionState.TIMED_OUT,
            updated_at=2.0,
            finished_at=2.0,
            reason=ExecutionReason.TIMEOUT,
        )
        for reason in (None, ExecutionReason.USER_CANCELLED):
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                self.record(
                    state=ExecutionState.TIMED_OUT,
                    updated_at=2.0,
                    finished_at=2.0,
                    reason=reason,
                )

        for reason in (
            ExecutionReason.USER_CANCELLED,
            ExecutionReason.CLIENT_CANCELLED,
            ExecutionReason.SERVER_SHUTDOWN,
        ):
            self.record(
                state=ExecutionState.CANCELLED,
                updated_at=2.0,
                finished_at=2.0,
                reason=reason,
            )
        for reason in (
            None,
            ExecutionReason.TIMEOUT,
            ExecutionReason.RUNTIME_START_FAILED,
        ):
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                self.record(
                    state=ExecutionState.CANCELLED,
                    updated_at=2.0,
                    finished_at=2.0,
                    reason=reason,
                )

        self.record(
            state=ExecutionState.SUCCEEDED,
            updated_at=2.0,
            finished_at=2.0,
        )
        with self.assertRaises(ValidationError):
            self.record(
                state=ExecutionState.SUCCEEDED,
                updated_at=2.0,
                finished_at=2.0,
                reason=ExecutionReason.TIMEOUT,
            )

    def test_legacy_status_mapping_keeps_reason_out_of_public_state(self) -> None:
        self.assertEqual(
            legacy_execution_status(
                ExecutionState.FAILED,
                ExecutionReason.WORKSPACE_LIMIT_EXCEEDED,
            ),
            "workspace_limit_exceeded",
        )
        self.assertEqual(
            legacy_execution_status(
                ExecutionState.FAILED,
                ExecutionReason.OUTPUT_LIMIT_EXCEEDED,
            ),
            "output_limit_exceeded",
        )
        self.assertEqual(
            legacy_execution_status(
                ExecutionState.CRASHED,
                ExecutionReason.RUNTIME_START_FAILED,
            ),
            "start_failed",
        )
        self.assertEqual(
            legacy_execution_status(
                ExecutionState.CANCELLED,
                ExecutionReason.USER_CANCELLED,
                service=True,
            ),
            "stopped",
        )


if __name__ == "__main__":
    unittest.main()
