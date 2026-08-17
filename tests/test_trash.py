from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sandboxed_workspace_mcp.access_policy import TRASH_DIRECTORY_NAME
from sandboxed_workspace_mcp.cli import parse_runtime
from sandboxed_workspace_mcp.config import ConfigurationError, Settings
from sandboxed_workspace_mcp.oauth import DEFAULT_OAUTH_SCOPES, OAuthSettings
from sandboxed_workspace_mcp.server import create_server
from sandboxed_workspace_mcp.trash import (
    TRASH_DESTINATION_EXISTS,
    TRASH_DESTINATION_INVALID,
    TRASH_ID_INVALID,
    TRASH_ITEM_NOT_FOUND,
    TRASH_STORAGE_CORRUPT,
    TRASH_VERSION_CONFLICT,
    TrashError,
    TrashManager,
)
from sandboxed_workspace_mcp.workspace import Workspace, WorkspaceError


class TrashManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings.create(
            self.root,
            allow_trash=True,
            max_trash_items=2,
            max_trash_bytes=32,
        )
        self.workspace = Workspace(self.settings)
        self.trash = TrashManager(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def test_trash_is_lazy_versioned_and_restorable(self) -> None:
        target = self.root / "notes.txt"
        data = b"recover me\n"
        target.write_bytes(data)

        self.assertFalse((self.root / TRASH_DIRECTORY_NAME).exists())
        version = self.workspace.read_file_versioned("notes.txt")
        result = self.trash.trash_file("notes.txt", version["sha256"])

        self.assertEqual(result["original_path"], "notes.txt")
        self.assertEqual(result["sha256"], self._sha256(data))
        self.assertFalse(target.exists())
        trash_root = self.root / TRASH_DIRECTORY_NAME
        self.assertEqual(stat.S_IMODE(trash_root.stat().st_mode), 0o700)
        listed = self.trash.list_trashed_files()
        self.assertEqual(listed["total"], 1)
        item = listed["items"][0]
        self.assertNotIn("payload", item)
        self.assertEqual(item["trash_id"], result["trash_id"])

        restored = self.trash.restore_trashed_file(result["trash_id"], result["sha256"])
        self.assertTrue(restored["restored"])
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(self.trash.list_trashed_files()["total"], 0)

    def test_stale_version_does_not_create_or_move_anything(self) -> None:
        target = self.root / "stale.txt"
        target.write_bytes(b"before")
        stale = self.workspace.read_file_versioned("stale.txt")["sha256"]
        target.write_bytes(b"after")

        with self.assertRaisesRegex(WorkspaceError, "conflict"):
            self.trash.trash_file("stale.txt", stale)
        self.assertEqual(target.read_bytes(), b"after")
        self.assertFalse((self.root / TRASH_DIRECTORY_NAME).exists())

    def test_symlink_directory_and_special_files_are_rejected(self) -> None:
        target = self.root / "target.txt"
        target.write_text("content", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks are unavailable: {exc}")
        version = self.workspace.read_file_versioned("target.txt")["sha256"]
        with self.assertRaisesRegex(WorkspaceError, "symbolic links"):
            self.trash.trash_file("link.txt", version)

        with self.assertRaisesRegex(WorkspaceError, "regular file"):
            self.trash.trash_file(".", version)
        with self.assertRaisesRegex(TrashError, "wildcard"):
            self.trash.trash_file("*.txt", version)

        if os.name == "posix":
            fifo = self.root / "pipe"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(WorkspaceError, "regular file"):
                self.trash.trash_file("pipe", version)

    def test_restore_never_overwrites_and_payload_remains(self) -> None:
        target = self.root / "collision.txt"
        target.write_bytes(b"original")
        version = self.workspace.read_file_versioned("collision.txt")["sha256"]
        item = self.trash.trash_file("collision.txt", version)
        target.write_bytes(b"new file")

        with self.assertRaisesRegex(TrashError, "already exists"):
            self.trash.restore_trashed_file(item["trash_id"], item["sha256"])
        self.assertEqual(target.read_bytes(), b"new file")
        self.assertEqual(self.trash.list_trashed_files()["total"], 1)

    def test_missing_and_invalid_ids_have_stable_codes(self) -> None:
        with self.assertRaises(TrashError) as invalid:
            self.trash.restore_trashed_file("not-an-id", "0" * 64)
        self.assertEqual(invalid.exception.code, TRASH_ID_INVALID)

        with self.assertRaises(TrashError) as missing:
            self.trash.restore_trashed_file("0" * 32, "0" * 64)
        self.assertEqual(missing.exception.code, TRASH_ITEM_NOT_FOUND)
        self.assertNotIn("cannot inspect trash item directory", str(missing.exception))

    def test_restore_to_alternate_path_preserves_new_original(self) -> None:
        target = self.root / "basic.txt"
        target.write_bytes(b"old")
        item = self.trash.trash_file(
            "basic.txt", self.workspace.read_file_versioned("basic.txt")["sha256"]
        )
        target.write_bytes(b"new")
        (self.root / "recovered").mkdir()

        result = self.trash.restore_trashed_file(
            item["trash_id"], item["sha256"], "recovered/basic.txt"
        )

        self.assertEqual(result["restored_path"], "recovered/basic.txt")
        self.assertFalse(result["restored_to_original"])
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual((self.root / "recovered/basic.txt").read_bytes(), b"old")

    def test_restore_to_invalid_or_existing_destination_keeps_item(self) -> None:
        target = self.root / "source.txt"
        target.write_bytes(b"old")
        item = self.trash.trash_file(
            "source.txt", self.workspace.read_file_versioned("source.txt")["sha256"]
        )
        (self.root / "recovered").mkdir()
        (self.root / "recovered/existing.txt").write_bytes(b"keep")

        with self.assertRaises(TrashError) as existing:
            self.trash.restore_trashed_file(
                item["trash_id"], item["sha256"], "recovered/existing.txt"
            )
        self.assertEqual(existing.exception.code, TRASH_DESTINATION_EXISTS)
        self.assertEqual((self.root / "recovered/existing.txt").read_bytes(), b"keep")

        with self.assertRaises(TrashError) as invalid:
            self.trash.restore_trashed_file(
                item["trash_id"], item["sha256"], "missing/file.txt"
            )
        self.assertEqual(invalid.exception.code, TRASH_DESTINATION_INVALID)
        self.assertEqual(self.trash.list_trashed_files()["total"], 1)

    def test_purge_is_sha_checked_and_releases_quota(self) -> None:
        settings = Settings.create(
            self.root,
            allow_trash=True,
            allow_trash_purge=True,
            max_trash_items=1,
            max_trash_bytes=32,
        )
        workspace = Workspace(settings)
        trash = TrashManager(workspace)
        target = self.root / "purge.txt"
        target.write_bytes(b"purge me")
        item = trash.trash_file(
            "purge.txt", workspace.read_file_versioned("purge.txt")["sha256"]
        )

        with self.assertRaises(TrashError) as stale:
            trash.purge_trashed_file(item["trash_id"], "0" * 64)
        self.assertEqual(stale.exception.code, TRASH_VERSION_CONFLICT)
        self.assertEqual(trash.list_trashed_files()["total"], 1)

        result = trash.purge_trashed_file(item["trash_id"], item["sha256"])
        self.assertTrue(result["purged"])
        self.assertFalse(result["cleanup_pending"])
        self.assertEqual(trash.list_trashed_files()["total"], 0)

    def test_missing_required_store_directory_is_storage_corrupt(self) -> None:
        trash_root = self.root / TRASH_DIRECTORY_NAME
        trash_root.mkdir()
        (trash_root / "format.json").write_text('{"version": 1}', encoding="utf-8")
        (trash_root / "staging").mkdir()

        with self.assertRaises(TrashError) as error:
            self.trash.restore_trashed_file("0" * 32, "0" * 64)
        self.assertEqual(error.exception.code, TRASH_STORAGE_CORRUPT)

    def test_corrupt_item_shapes_have_typed_errors(self) -> None:
        target = self.root / "corrupt.txt"
        target.write_bytes(b"payload")
        item = self.trash.trash_file(
            "corrupt.txt", self.workspace.read_file_versioned("corrupt.txt")["sha256"]
        )
        item_dir = self.root / TRASH_DIRECTORY_NAME / "items" / item["trash_id"]
        (item_dir / "payload").unlink()

        with self.assertRaises(TrashError) as missing_payload:
            self.trash.restore_trashed_file(item["trash_id"], item["sha256"])
        self.assertEqual(missing_payload.exception.code, "TRASH_ITEM_CORRUPT")

        target.write_bytes(b"payload")
        item = self.trash.trash_file(
            "corrupt.txt", self.workspace.read_file_versioned("corrupt.txt")["sha256"]
        )
        item_dir = self.root / TRASH_DIRECTORY_NAME / "items" / item["trash_id"]
        (item_dir / "metadata.json").write_bytes(b"not json")
        with self.assertRaises(TrashError) as bad_metadata:
            self.trash.restore_trashed_file(item["trash_id"], item["sha256"])
        self.assertEqual(bad_metadata.exception.code, "TRASH_ITEM_CORRUPT")

    def test_restore_intent_recovery_handles_commit_boundaries(self) -> None:
        settings = Settings.create(
            self.root, allow_trash=True, max_trash_items=10, max_trash_bytes=128
        )
        workspace = Workspace(settings)
        trash = TrashManager(workspace)
        recovered = self.root / "recovered"
        recovered.mkdir()

        def add_item(name: str) -> dict[str, object]:
            target = self.root / name
            target.write_bytes(name.encode())
            return trash.trash_file(name, workspace.read_file_versioned(name)["sha256"])

        def write_intent(item: dict[str, object], destination: str) -> Path:
            item_dir = self.root / TRASH_DIRECTORY_NAME / "items" / item["trash_id"]
            (item_dir / "restore-intent.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "trash_id": item["trash_id"],
                        "destination_path": destination,
                        "sha256": item["sha256"],
                    }
                ),
                encoding="utf-8",
            )
            return item_dir

        committed = add_item("committed.txt")
        committed_dir = write_intent(committed, "recovered/committed.txt")
        os.link(committed_dir / "payload", recovered / "committed.txt")
        self.assertEqual(trash.list_trashed_files()["total"], 0)

        pending = add_item("pending.txt")
        write_intent(pending, "recovered/pending.txt")
        self.assertEqual(trash.list_trashed_files()["total"], 1)
        self.assertFalse(
            (
                self.root
                / TRASH_DIRECTORY_NAME
                / "items"
                / pending["trash_id"]
                / "restore-intent.json"
            ).exists()
        )

        completed = add_item("completed.txt")
        completed_dir = write_intent(completed, "recovered/completed.txt")
        os.link(completed_dir / "payload", recovered / "completed.txt")
        (completed_dir / "payload").unlink()
        self.assertEqual(trash.list_trashed_files()["total"], 1)

        conflict = add_item("conflict.txt")
        write_intent(conflict, "recovered/conflict.txt")
        (recovered / "conflict.txt").write_bytes(b"different")
        listed = trash.list_trashed_files()
        self.assertEqual(listed["total"], 2)
        self.assertIn("diagnostics", listed)

    def test_restore_recovery_finishes_after_metadata_and_payload_cleanup(self) -> None:
        target = self.root / "finalize.txt"
        data = b"finalized restore"
        target.write_bytes(data)
        item = self.trash.trash_file(
            "finalize.txt", self.workspace.read_file_versioned("finalize.txt")["sha256"]
        )
        trash_id = item["trash_id"]
        sha256 = item["sha256"]
        self.assertIsInstance(trash_id, str)
        self.assertIsInstance(sha256, str)

        recovered = self.root / "recovered"
        recovered.mkdir()
        destination = recovered / "finalize.txt"
        item_dir = self.root / TRASH_DIRECTORY_NAME / "items" / trash_id
        (item_dir / "restore-intent.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "trash_id": trash_id,
                    "destination_path": "recovered/finalize.txt",
                    "sha256": sha256,
                }
            ),
            encoding="utf-8",
        )
        os.link(item_dir / "payload", destination)
        (item_dir / "payload").unlink()
        (item_dir / "metadata.json").unlink()

        listed = self.trash.list_trashed_files()

        self.assertEqual(listed["total"], 0)
        self.assertFalse(item_dir.exists())
        self.assertEqual(destination.read_bytes(), data)

    def test_restore_recovery_preserves_item_with_corrupt_intent_schema(self) -> None:
        target = self.root / "schema.txt"
        target.write_bytes(b"schema payload")
        item = self.trash.trash_file(
            "schema.txt", self.workspace.read_file_versioned("schema.txt")["sha256"]
        )
        trash_id = item["trash_id"]
        sha256 = item["sha256"]
        self.assertIsInstance(trash_id, str)
        self.assertIsInstance(sha256, str)
        item_dir = self.root / TRASH_DIRECTORY_NAME / "items" / trash_id
        (item_dir / "restore-intent.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "trash_id": trash_id,
                    "destination_path": 123,
                    "sha256": sha256,
                }
            ),
            encoding="utf-8",
        )

        listed = self.trash.list_trashed_files()

        self.assertEqual(listed["total"], 0)
        self.assertIn("restore intent recovery is pending", listed["diagnostics"])
        self.assertTrue((item_dir / "payload").exists())
        self.assertTrue((item_dir / "restore-intent.json").exists())

    def test_restore_recovery_rejects_payload_checksum_mismatch(self) -> None:
        target = self.root / "checksum.txt"
        target.write_bytes(b"expected")
        item = self.trash.trash_file(
            "checksum.txt", self.workspace.read_file_versioned("checksum.txt")["sha256"]
        )
        trash_id = item["trash_id"]
        sha256 = item["sha256"]
        self.assertIsInstance(trash_id, str)
        self.assertIsInstance(sha256, str)

        recovered = self.root / "recovered"
        recovered.mkdir()
        item_dir = self.root / TRASH_DIRECTORY_NAME / "items" / trash_id
        (item_dir / "restore-intent.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "trash_id": trash_id,
                    "destination_path": "recovered/checksum.txt",
                    "sha256": sha256,
                }
            ),
            encoding="utf-8",
        )
        (item_dir / "payload").write_bytes(b"tampered")

        listed = self.trash.list_trashed_files()

        self.assertEqual(listed["total"], 1)
        self.assertIn(
            "restore intent payload checksum is invalid", listed["diagnostics"]
        )
        self.assertFalse((recovered / "checksum.txt").exists())
        self.assertEqual((item_dir / "payload").read_bytes(), b"tampered")

    def test_storage_shape_corruption_has_stable_typed_errors(self) -> None:
        cases = ("root-file", "bad-format", "purging-file")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = Settings.create(root, allow_trash=True)
                trash = TrashManager(Workspace(settings))
                trash_root = root / TRASH_DIRECTORY_NAME

                if case == "root-file":
                    trash_root.write_text("not a directory", encoding="utf-8")
                else:
                    trash_root.mkdir(mode=0o700)
                    (trash_root / "format.json").write_text(
                        json.dumps({"version": 99 if case == "bad-format" else 1}),
                        encoding="utf-8",
                    )
                    (trash_root / "staging").mkdir(mode=0o700)
                    (trash_root / "items").mkdir(mode=0o700)
                    if case == "purging-file":
                        (trash_root / "purging").write_text(
                            "not a directory", encoding="utf-8"
                        )

                with self.assertRaises(TrashError) as error:
                    trash.list_trashed_files()
                self.assertEqual(error.exception.code, TRASH_STORAGE_CORRUPT)

    def test_purge_recovery_diagnoses_malformed_transaction_entries(self) -> None:
        settings = Settings.create(
            self.root,
            allow_trash=True,
            max_trash_items=10,
            max_trash_bytes=128,
        )
        trash = TrashManager(Workspace(settings))
        store = trash._ensure_store(create=True)
        purging = trash._ensure_purging(store)
        (purging / "bad-name").mkdir()
        (purging / ("a" * 32)).write_text("not a directory", encoding="utf-8")
        pending = purging / ("b" * 32)
        pending.mkdir()

        diagnostics: list[str] = []
        with patch.object(trash, "_cleanup_purging_item", return_value=False):
            trash._recover_purging(purging, diagnostics)

        self.assertIn("invalid purge transaction entry name", diagnostics)
        self.assertIn("purge transaction item is not a real directory", diagnostics)
        self.assertIn("purge transaction cleanup is pending", diagnostics)

        with patch(
            "sandboxed_workspace_mcp.trash.os.scandir",
            side_effect=OSError("simulated scan failure"),
        ):
            diagnostics = []
            trash._recover_purging(purging, diagnostics)
        self.assertEqual(diagnostics, ["cannot scan purge transaction directory"])

    def test_staging_recovery_handles_commit_conflict_and_partial_moves(self) -> None:
        cases = ("complete", "conflict", "original-restored", "incomplete")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = Settings.create(
                    root,
                    allow_trash=True,
                    max_trash_items=10,
                    max_trash_bytes=128,
                )
                workspace = Workspace(settings)
                trash = TrashManager(workspace)
                source = root / "source.txt"
                data = b"payload"
                source.write_bytes(data)
                item = trash.trash_file(
                    "source.txt", workspace.read_file_versioned("source.txt")["sha256"]
                )
                trash_id = item["trash_id"]
                self.assertIsInstance(trash_id, str)
                trash_root = root / TRASH_DIRECTORY_NAME
                formal = trash_root / "items" / trash_id
                staging = trash_root / "staging" / trash_id
                os.replace(formal, staging)

                if case == "conflict":
                    formal.mkdir()
                elif case == "original-restored":
                    source.write_bytes(data)
                    (staging / "payload").unlink()
                elif case == "incomplete":
                    (staging / "payload").unlink()

                listed = trash.list_trashed_files()

                if case == "complete":
                    self.assertEqual(listed["total"], 1)
                    self.assertTrue(formal.is_dir())
                    self.assertFalse(staging.exists())
                elif case == "conflict":
                    self.assertIn(
                        "staging item conflicts with formal item", listed["diagnostics"]
                    )
                    self.assertTrue(staging.is_dir())
                elif case == "original-restored":
                    self.assertEqual(listed["total"], 0)
                    self.assertFalse(staging.exists())
                    self.assertEqual(source.read_bytes(), data)
                else:
                    self.assertEqual(listed["total"], 0)
                    self.assertIn("incomplete staging item", listed["diagnostics"])
                    self.assertTrue(staging.is_dir())

    def test_formal_recovery_preserves_unverifiable_states(self) -> None:
        cases = ("missing-metadata", "missing-payload", "checksum-mismatch")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings = Settings.create(root, allow_trash=True)
                workspace = Workspace(settings)
                trash = TrashManager(workspace)
                source = root / "source.txt"
                source.write_bytes(b"payload")
                item = trash.trash_file(
                    "source.txt", workspace.read_file_versioned("source.txt")["sha256"]
                )
                trash_id = item["trash_id"]
                self.assertIsInstance(trash_id, str)
                item_dir = root / TRASH_DIRECTORY_NAME / "items" / trash_id

                if case == "missing-metadata":
                    (item_dir / "metadata.json").unlink()
                elif case == "missing-payload":
                    (item_dir / "payload").unlink()
                else:
                    source.write_bytes(b"payload")
                    (item_dir / "payload").write_bytes(b"payloae")

                listed = trash.list_trashed_files()

                expected = {
                    "missing-metadata": "formal item has no metadata",
                    "missing-payload": "formal item is missing its payload",
                    "checksum-mismatch": "formal item payload checksum is invalid",
                }[case]
                self.assertIn(expected, listed["diagnostics"])
                self.assertTrue(item_dir.exists())

    def test_metadata_schema_validation_rejects_corrupt_fields(self) -> None:
        source = self.root / "metadata.txt"
        source.write_bytes(b"payload")
        item = self.trash.trash_file(
            "metadata.txt", self.workspace.read_file_versioned("metadata.txt")["sha256"]
        )
        trash_id = item["trash_id"]
        self.assertIsInstance(trash_id, str)
        metadata_path = (
            self.root / TRASH_DIRECTORY_NAME / "items" / trash_id / "metadata.json"
        )
        valid = json.loads(metadata_path.read_text(encoding="utf-8"))

        cases = (
            ("version", {**valid, "version": 99}, "metadata version"),
            ("trash-id", {**valid, "trash_id": "f" * 32}, "metadata id"),
            ("original-empty", {**valid, "original_path": ""}, "original_path"),
            (
                "original-parent",
                {**valid, "original_path": "../escape"},
                "original_path",
            ),
            ("sha", {**valid, "sha256": "not-a-sha"}, "invalid sha256"),
            ("size-type", {**valid, "size": True}, "invalid size"),
            ("mtime-negative", {**valid, "mtime_ns": -1}, "invalid mtime_ns"),
            ("mode-large", {**valid, "mode": 0o10000}, "mode or timestamp"),
            ("timestamp-zero", {**valid, "trashed_at": 0}, "mode or timestamp"),
        )
        for name, value, message in cases:
            with self.subTest(name=name):
                metadata_path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(TrashError, message):
                    self.trash._read_metadata(metadata_path, trash_id)

    def test_json_metadata_reader_rejects_unsafe_shapes_and_encodings(self) -> None:
        path = self.root / "metadata-fixture.json"
        cases = (
            ("array", b"[]", "JSON object"),
            ("invalid-json", b"{", "valid UTF-8 JSON"),
            ("invalid-utf8", b"\xff", "valid UTF-8 JSON"),
            ("oversized", b"{} " * 10, "too large"),
        )
        for name, payload, message in cases:
            with self.subTest(name=name):
                path.unlink(missing_ok=True)
                path.write_bytes(payload)
                limit = 4 if name == "oversized" else 128
                with self.assertRaisesRegex(TrashError, message):
                    self.trash._read_json(path, limit)

        path.unlink()
        directory = self.root / "metadata-dir"
        directory.mkdir()
        with self.assertRaisesRegex(TrashError, "regular file"):
            self.trash._read_json(directory, 128)

        target = self.root / "metadata-target.json"
        target.write_text("{}", encoding="utf-8")
        path.symlink_to(target)
        with self.assertRaisesRegex(TrashError, "regular file"):
            self.trash._read_json(path, 128)

    def test_purge_cleanup_pending_is_recovered_without_relisting_item(self) -> None:
        target = self.root / "pending-purge.txt"
        target.write_bytes(b"payload")
        item = self.trash.trash_file(
            "pending-purge.txt",
            self.workspace.read_file_versioned("pending-purge.txt")["sha256"],
        )
        settings = Settings.create(
            self.root,
            allow_trash=True,
            allow_trash_purge=True,
            max_trash_items=2,
            max_trash_bytes=64,
        )
        trash = TrashManager(Workspace(settings))
        with patch.object(trash, "_cleanup_purging_item", return_value=True):
            result = trash.purge_trashed_file(item["trash_id"], item["sha256"])
        self.assertTrue(result["cleanup_pending"])
        self.assertEqual(trash.list_trashed_files()["total"], 0)

    def test_limits_reject_without_purging_existing_items(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_bytes(b"1234567890")
        second.write_bytes(b"abcdefghij")
        first_sha = self.workspace.read_file_versioned("first.txt")["sha256"]
        second_sha = self.workspace.read_file_versioned("second.txt")["sha256"]
        self.trash.trash_file("first.txt", first_sha)
        self.trash.trash_file("second.txt", second_sha)

        third = self.root / "third.txt"
        third.write_bytes(b"x")
        third_sha = self.workspace.read_file_versioned("third.txt")["sha256"]
        with self.assertRaisesRegex(TrashError, "limit"):
            self.trash.trash_file("third.txt", third_sha)
        self.assertTrue(third.exists())
        self.assertEqual(self.trash.list_trashed_files()["total"], 2)

    def test_malformed_entries_are_bounded_diagnostics(self) -> None:
        target = self.root / "safe.txt"
        target.write_text("safe", encoding="utf-8")
        sha = self.workspace.read_file_versioned("safe.txt")["sha256"]
        self.trash.trash_file("safe.txt", sha)
        trash_root = self.root / TRASH_DIRECTORY_NAME
        bad_item = trash_root / "items" / ("a" * 32)
        bad_item.mkdir(mode=0o700)
        (bad_item / "metadata.json").write_bytes(b"not json")

        listed = self.trash.list_trashed_files(limit=1)
        self.assertEqual(listed["total"], 1)
        self.assertIn("diagnostics", listed)
        self.assertLessEqual(len(listed["diagnostics"]), 21)

    def test_recovery_commits_complete_staging_and_cleans_completed_restore(
        self,
    ) -> None:
        target = self.root / "staged.txt"
        target.write_bytes(b"staged payload")
        sha = self.workspace.read_file_versioned("staged.txt")["sha256"]
        item = self.trash.trash_file("staged.txt", sha)
        trash_root = self.root / TRASH_DIRECTORY_NAME
        formal_item = trash_root / "items" / item["trash_id"]
        staging_item = trash_root / "staging" / item["trash_id"]
        os.replace(formal_item, staging_item)

        listed = self.trash.list_trashed_files()
        self.assertEqual(listed["total"], 1)
        self.assertTrue((trash_root / "items" / item["trash_id"]).is_dir())

        target.write_bytes(b"staged payload")
        payload = trash_root / "items" / item["trash_id"] / "payload"
        payload.unlink()
        self.assertEqual(self.trash.list_trashed_files()["total"], 0)
        self.assertEqual(target.read_bytes(), b"staged payload")

    def test_reserved_directory_is_not_visible_to_workspace(self) -> None:
        target = self.root / "hidden.txt"
        target.write_text("secret", encoding="utf-8")
        sha = self.workspace.read_file_versioned("hidden.txt")["sha256"]
        self.trash.trash_file("hidden.txt", sha)

        self.assertNotIn(TRASH_DIRECTORY_NAME, self.workspace.list_directory())
        self.assertNotIn(TRASH_DIRECTORY_NAME, self.workspace.tree())
        self.assertNotIn("secret", self.workspace.search_text("secret"))
        with self.assertRaisesRegex(WorkspaceError, "blocked"):
            self.workspace.list_directory(TRASH_DIRECTORY_NAME)

    def test_existing_unknown_reserved_directory_is_not_adopted(self) -> None:
        (self.root / TRASH_DIRECTORY_NAME).mkdir()
        with self.assertRaisesRegex(TrashError, "format marker"):
            self.trash.list_trashed_files()
        self.assertFalse((self.root / TRASH_DIRECTORY_NAME / "format.json").exists())


class TrashConfigurationAndServerTests(unittest.TestCase):
    def test_defaults_and_read_only_configuration_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = Settings.create(root)
            self.assertFalse(settings.allow_trash)
            self.assertFalse(settings.allow_trash_purge)
            self.assertEqual(settings.max_trash_items, 200)
            self.assertEqual(settings.max_trash_bytes, 256 * 1024 * 1024)
            with self.assertRaisesRegex(ConfigurationError, "allow_trash"):
                Settings.create(root, allow_writes=False, allow_trash=True)
            with self.assertRaisesRegex(ConfigurationError, "allow_trash_purge"):
                Settings.create(root, allow_trash_purge=True)
            with self.assertRaisesRegex(ConfigurationError, "allow_writes"):
                Settings.create(
                    root,
                    allow_writes=False,
                    allow_trash=True,
                    allow_trash_purge=True,
                )

            for values, message in (
                ({"max_trash_items": 0}, "max_trash_items"),
                ({"max_trash_bytes": 0}, "max_trash_bytes"),
                ({"max_trash_items": 10_001}, "max_trash_items"),
                ({"max_trash_bytes": 4 * 1024 * 1024 * 1024 + 1}, "max_trash_bytes"),
            ):
                with (
                    self.subTest(values=values),
                    self.assertRaisesRegex(ConfigurationError, message),
                ):
                    Settings.create(root, **values)

    def test_cli_flags_and_environment_configure_trash(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = parse_runtime(
                ["--allow-trash", "--max-trash-items", "3", "--max-trash-bytes", "99"],
                {"SANDBOXED_WORKSPACE_MCP_ROOT": root},
            )
            self.assertTrue(runtime.settings.allow_trash)
            self.assertEqual(runtime.settings.max_trash_items, 3)
            self.assertEqual(runtime.settings.max_trash_bytes, 99)
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parse_runtime(
                    ["--read-only", "--allow-trash"],
                    {"SANDBOXED_WORKSPACE_MCP_ROOT": root},
                )

            purge_runtime = parse_runtime(
                ["--allow-trash", "--allow-trash-purge"],
                {"SANDBOXED_WORKSPACE_MCP_ROOT": root},
            )
            self.assertTrue(purge_runtime.settings.allow_trash_purge)
            env_runtime = parse_runtime(
                [],
                {
                    "SANDBOXED_WORKSPACE_MCP_ROOT": root,
                    "SANDBOXED_WORKSPACE_MCP_ALLOW_TRASH": "true",
                    "SANDBOXED_WORKSPACE_MCP_ALLOW_TRASH_PURGE": "true",
                },
            )
            self.assertTrue(env_runtime.settings.allow_trash_purge)

    def test_delete_scope_is_opt_in_and_required_when_trash_is_enabled(self) -> None:
        self.assertNotIn("workspace.delete", DEFAULT_OAUTH_SCOPES)
        oauth = OAuthSettings(
            issuer="https://idp.example.test/tenant",
            audience="https://mcp.example.test",
            public_origin="https://mcp.example.test",
            jwks_uri="https://idp.example.test/keys",
        )
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "workspace.delete"):
                create_server(Settings.create(root, allow_trash=True), oauth=oauth)

            enabled_oauth = OAuthSettings(
                issuer=oauth.issuer,
                audience=oauth.audience,
                public_origin=oauth.public_origin,
                jwks_uri=oauth.jwks_uri,
                scopes=(*oauth.scopes, "workspace.delete"),
            )
            server = create_server(
                Settings.create(root, allow_trash=True), oauth=enabled_oauth
            )
            tools = {tool.name: tool for tool in self._list_tools(server)}
            self.assertEqual(
                tools["trash_file"].meta["securitySchemes"],
                [{"type": "oauth2", "scopes": ["workspace.delete"]}],
            )

    def test_trash_tools_only_register_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            disabled = create_server(Settings.create(root))
            disabled_names = {tool.name for tool in self._list_tools(disabled)}
            self.assertTrue(
                disabled_names.isdisjoint(
                    {"trash_file", "list_trashed_files", "restore_trashed_file"}
                )
            )

            enabled = create_server(Settings.create(root, allow_trash=True))
            tools = {tool.name: tool for tool in self._list_tools(enabled)}
            self.assertTrue(
                {"trash_file", "list_trashed_files", "restore_trashed_file"}
                <= tools.keys()
            )
            self.assertTrue(tools["trash_file"].annotations.destructive_hint)
            self.assertTrue(tools["restore_trashed_file"].annotations.destructive_hint)

            result = self._call(enabled, "list_trashed_files", {"limit": 1})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["total"], 0)

    def test_server_returns_structured_trash_errors(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root, allow_trash=True))
            result = self._call(
                server,
                "restore_trashed_file",
                {"trash_id": "bad", "expected_sha256": "0" * 64},
            )

        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], TRASH_ID_INVALID)
        self.assertTrue(result.content)

    def test_restore_to_and_purge_have_separate_registration_and_scopes(self) -> None:
        oauth = OAuthSettings(
            issuer="https://idp.example.test/tenant",
            audience="https://mcp.example.test",
            public_origin="https://mcp.example.test",
            jwks_uri="https://idp.example.test/keys",
            scopes=(
                *DEFAULT_OAUTH_SCOPES,
                "workspace.delete",
                "workspace.purge",
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            server = create_server(
                Settings.create(root, allow_trash=True, allow_trash_purge=True),
                oauth=oauth,
            )
            tools = {tool.name: tool for tool in self._list_tools(server)}

        self.assertIn("restore_trashed_file_to", tools)
        self.assertIn("purge_trashed_file", tools)
        self.assertEqual(
            tools["restore_trashed_file_to"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["workspace.delete", "workspace.write"]}],
        )
        self.assertEqual(
            tools["purge_trashed_file"].meta["securitySchemes"],
            [{"type": "oauth2", "scopes": ["workspace.delete", "workspace.purge"]}],
        )

    def test_restore_to_metadata_emphasizes_recovery_intent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = create_server(Settings.create(root, allow_trash=True))
            tools = {tool.name: tool for tool in self._list_tools(server)}

        server_description = server.description.lower()
        self.assertIn("recycle-bin trash", server_description)
        self.assertIn("recovery", server_description)
        self.assertIn("alternate-path restore", server_description)

        description = tools["restore_trashed_file_to"].description.lower()
        first_sentence = description.splitlines()[0]
        self.assertIn("restore or recover", first_sentence)
        self.assertIn("trashed recycle-bin file", first_sentence)
        self.assertIn("alternate path", first_sentence)
        self.assertNotIn("delete permission", description)

    @staticmethod
    def _list_tools(server):
        return asyncio.run(server.list_tools())

    @staticmethod
    def _call(server, name: str, arguments: dict[str, object]):
        return asyncio.run(server.call_tool(name, arguments))


if __name__ == "__main__":
    unittest.main()
