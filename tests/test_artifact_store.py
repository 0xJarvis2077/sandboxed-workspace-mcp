from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from workspace_guard_mcp.artifact import ArtifactRecord
from workspace_guard_mcp.artifact_store import (
    ArtifactLimitExceeded,
    ArtifactPolicyViolation,
    ArtifactResourceTooLarge,
    ArtifactStoreMiss,
    EphemeralArtifactStore,
)
from workspace_guard_mcp.task_config import TaskLimits


class ArtifactRecordTests(unittest.TestCase):
    def _record(self, **overrides: object) -> ArtifactRecord:
        values: dict[str, object] = {
            "artifact_id": "A" * 32,
            "execution_id": "execution-1",
            "name": "coverage.xml",
            "media_type": "application/xml",
            "size_bytes": 12,
            "sha256": "a" * 64,
            "created_at": 1.0,
        }
        values.update(overrides)
        return ArtifactRecord(**values)  # type: ignore[arg-type]

    def test_valid_record_is_frozen_and_forbids_extra(self) -> None:
        record = self._record()
        self.assertEqual(record.name, "coverage.xml")
        with self.assertRaises(ValidationError):
            self._record(extra_field=True)
        with self.assertRaises(ValidationError):
            record.name = "other.txt"  # type: ignore[misc]

    def test_rejects_unsafe_or_overlong_names(self) -> None:
        for name in (
            "",
            "../x",
            "a/b",
            "a\\b",
            "nul\x00x",
            "line\nfeed",
            "report\u202egnp.exe",
        ):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self._record(name=name)
        with self.assertRaises(ValidationError):
            self._record(name="é" * 256)
        self.assertEqual(self._record(name="报告📊.csv").name, "报告📊.csv")

    def test_rejects_invalid_ids_hash_sizes_and_time(self) -> None:
        invalid = (
            {"artifact_id": "short"},
            {"sha256": "A" * 64},
            {"sha256": "a" * 63},
            {"size_bytes": -1},
            {"size_bytes": True},
            {"created_at": float("nan")},
            {"created_at": float("inf")},
            {"media_type": "not-a-mime"},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self._record(**values)


class EphemeralArtifactStoreTests(unittest.TestCase):
    def _limits(self, **overrides: object) -> TaskLimits:
        values: dict[str, object] = {
            "max_artifacts_per_execution": 3,
            "max_artifact_bytes": 8,
            "max_total_artifact_bytes": 12,
        }
        values.update(overrides)
        return TaskLimits(**values)  # type: ignore[arg-type]

    def test_binary_and_zero_byte_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            binary = b"\x00\xff\x10abc"
            (staging / "image.bin").write_bytes(binary)
            (staging / "empty.txt").write_bytes(b"")
            store = EphemeralArtifactStore()
            records = store.collect("execution-1", staging, self._limits())

        by_name = {record.name: record for record in records}
        binary_record = by_name["image.bin"]
        self.assertEqual(binary_record.size_bytes, len(binary))
        self.assertEqual(binary_record.sha256, hashlib.sha256(binary).hexdigest())
        self.assertEqual(binary_record.media_type, "application/octet-stream")
        self.assertEqual(store.read(binary_record.artifact_id), binary)
        empty_record = by_name["empty.txt"]
        self.assertEqual(empty_record.size_bytes, 0)
        self.assertEqual(empty_record.sha256, hashlib.sha256(b"").hexdigest())
        self.assertEqual(store.read(empty_record.artifact_id), b"")

    def test_exact_file_total_and_count_limits_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            (staging / "a.bin").write_bytes(b"a" * 8)
            (staging / "b.bin").write_bytes(b"b" * 4)
            store = EphemeralArtifactStore()
            records = store.collect("execution-1", staging, self._limits())
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(record.size_bytes for record in records), 12)

    def test_limit_violations_publish_nothing(self) -> None:
        cases = (
            (
                {"max_artifact_bytes": 7},
                {"a.bin": b"a" * 8},
            ),
            (
                {"max_artifacts_per_execution": 1},
                {"a.bin": b"a", "b.bin": b"b"},
            ),
            (
                {"max_total_artifact_bytes": 7, "max_artifact_bytes": 7},
                {"a.bin": b"a" * 4, "b.bin": b"b" * 4},
            ),
        )
        for index, (limit_overrides, files) in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                staging = Path(directory)
                for name, content in files.items():
                    (staging / name).write_bytes(content)
                store = EphemeralArtifactStore()
                with self.assertRaises(ArtifactLimitExceeded):
                    store.collect(
                        f"execution-{index}",
                        staging,
                        self._limits(**limit_overrides),
                    )
                self.assertEqual(store.execution_count, 0)
                self.assertEqual(store.total_bytes, 0)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlink_is_policy_violation_and_target_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            (staging / "good.txt").write_text("ok", encoding="utf-8")
            os.symlink(outside, staging / "escape.txt")
            store = EphemeralArtifactStore()
            with self.assertRaises(ArtifactPolicyViolation):
                store.collect("execution-1", staging, self._limits())
            self.assertEqual(store.execution_count, 0)
            self.assertEqual(store.total_bytes, 0)

    def test_directory_is_policy_violation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            (staging / "report").mkdir()
            store = EphemeralArtifactStore()
            with self.assertRaises(ArtifactPolicyViolation):
                store.collect("execution-1", staging, self._limits())

    def test_owner_isolation_and_ttl_expiry(self) -> None:
        now = [10.0]
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            (staging / "a.txt").write_text("abc", encoding="utf-8")
            store = EphemeralArtifactStore(ttl_seconds=5, clock=lambda: now[0])
            record = store.collect(
                "execution-1", staging, self._limits(), owner_scope="owner-a"
            )[0]
            self.assertEqual(
                store.list_execution("execution-1", owner_scope="owner-a"), [record]
            )
            self.assertEqual(
                store.list_execution("execution-1", owner_scope="owner-b"), []
            )
            with self.assertRaises(ArtifactStoreMiss):
                store.read(record.artifact_id, owner_scope="owner-b")
            now[0] = 16.0
            with self.assertRaises(ArtifactStoreMiss):
                store.read(record.artifact_id, owner_scope="owner-a")
            self.assertEqual(store.execution_count, 0)

    def test_direct_resource_read_rejects_oversized_content_before_read_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            (staging / "large.bin").write_bytes(b"12345")
            store = EphemeralArtifactStore()
            record = store.collect("execution-1", staging, self._limits())[0]
        with (
            patch(
                "workspace_guard_mcp.artifact_store.MAX_ARTIFACT_RESOURCE_BYTES",
                4,
            ),
            patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("must not read"),
            ),
        ):
            with self.assertRaisesRegex(
                ArtifactResourceTooLarge,
                "artifact too large for direct resource delivery",
            ):
                store.read(record.artifact_id)

    def test_retention_evicts_whole_oldest_execution_set(self) -> None:
        store = EphemeralArtifactStore(max_retained_executions=1, max_store_bytes=32)
        records = []
        for execution_id, value in (("one", b"1"), ("two", b"22")):
            with tempfile.TemporaryDirectory() as directory:
                staging = Path(directory)
                (staging / "a.bin").write_bytes(value)
                records.append(store.collect(execution_id, staging, self._limits())[0])
        self.assertEqual(store.execution_count, 1)
        self.assertEqual(store.list_execution("one"), [])
        with self.assertRaises(ArtifactStoreMiss):
            store.read(records[0].artifact_id)
        self.assertEqual(store.read(records[1].artifact_id), b"22")

    def test_store_byte_limit_evicts_whole_oldest_execution_set(self) -> None:
        store = EphemeralArtifactStore(max_retained_executions=8, max_store_bytes=5)
        first: ArtifactRecord
        second: ArtifactRecord
        with tempfile.TemporaryDirectory() as first_dir:
            staging = Path(first_dir)
            (staging / "a.bin").write_bytes(b"123")
            first = store.collect("one", staging, self._limits())[0]
        with tempfile.TemporaryDirectory() as second_dir:
            staging = Path(second_dir)
            (staging / "b.bin").write_bytes(b"456")
            second = store.collect("two", staging, self._limits())[0]
        with self.assertRaises(ArtifactStoreMiss):
            store.read(first.artifact_id)
        self.assertEqual(store.read(second.artifact_id), b"456")
        self.assertEqual(store.total_bytes, 3)


if __name__ == "__main__":
    unittest.main()
