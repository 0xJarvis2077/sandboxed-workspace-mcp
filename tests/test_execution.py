from __future__ import annotations

import unittest

from pydantic import ValidationError

from workspace_guard_mcp.execution import (
    ExecutionKind,
    ExecutionMode,
    ExecutionReason,
    ExecutionRecord,
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
