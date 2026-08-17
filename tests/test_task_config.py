from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from workspace_guard_mcp.cli import parse_runtime
from workspace_guard_mcp.task_config import (
    TaskConfigurationError,
    load_task_config,
)

PINNED_IMAGE = "example.invalid/workspace-guard-mcp@sha256:" + "a" * 64


class TaskConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.config_path = self.base / "tasks.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, value: object) -> Path:
        self.config_path.write_text(json.dumps(value), encoding="utf-8")
        return self.config_path

    def _valid(self) -> dict[str, object]:
        return {
            "version": 1,
            "runtime": "docker",
            "limits": {
                "timeout_seconds": 10,
                "max_output_bytes": 4096,
                "max_snapshot_files": 20,
                "max_snapshot_bytes": 100_000,
                "memory": "512m",
                "cpus": "1.5",
                "pids": 32,
                "max_concurrent_tasks": 2,
            },
            "tasks": {
                "test": {
                    "mode": "run",
                    "image": PINNED_IMAGE,
                    "argv": ["python", "-m", "unittest"],
                },
                "dev-service": {
                    "mode": "service",
                    "image": PINNED_IMAGE,
                    "argv": ["python", "-m", "example_app"],
                },
            },
        }

    def test_valid_configuration_is_loaded_and_frozen(self) -> None:
        configuration = load_task_config(
            self._write(self._valid()), workspace_root=self.workspace
        )

        self.assertEqual(configuration.runtime, "docker")
        self.assertEqual(configuration.limits.cpus, "1.5")
        self.assertEqual(configuration.tasks["test"].argv[0], "python")
        with self.assertRaises(TypeError):
            configuration.tasks["other"] = configuration.tasks["test"]  # type: ignore[index]

        self.config_path.write_text("{}", encoding="utf-8")
        self.assertIn("test", configuration.tasks)

    def test_workspace_access_defaults_read_only_and_writable_is_explicit(self) -> None:
        configuration = load_task_config(
            self._write(self._valid()), workspace_root=self.workspace
        )
        self.assertEqual(configuration.tasks["test"].workspace_access, "read-only")

        value = self._valid()
        tasks = value["tasks"]
        assert isinstance(tasks, dict)
        task = tasks["test"]
        assert isinstance(task, dict)
        task["workspace_access"] = "writable"
        with self.assertRaisesRegex(TaskConfigurationError, "explicit limit"):
            load_task_config(self._write(value), workspace_root=self.workspace)

        limits = value["limits"]
        assert isinstance(limits, dict)
        limits.update(
            {
                "max_workspace_file_bytes": 1024 * 1024,
                "max_workspace_growth_bytes": 10 * 1024 * 1024,
                "allow_best_effort_disk_limit": False,
            }
        )
        with self.assertRaisesRegex(TaskConfigurationError, "require.*true"):
            load_task_config(self._write(value), workspace_root=self.workspace)

        limits["allow_best_effort_disk_limit"] = True
        configuration = load_task_config(
            self._write(value), workspace_root=self.workspace
        )
        self.assertEqual(configuration.tasks["test"].workspace_access, "writable")

    def test_full_local_sha256_image_id_is_an_immutable_reference(self) -> None:
        value = self._valid()
        tasks = value["tasks"]
        assert isinstance(tasks, dict)
        for task in tasks.values():
            assert isinstance(task, dict)
            task["image"] = "sha256:" + "f" * 64

        configuration = load_task_config(
            self._write(value), workspace_root=self.workspace
        )

        self.assertEqual(configuration.tasks["test"].image, "sha256:" + "f" * 64)

    def test_cli_and_environment_load_task_config_once(self) -> None:
        self._write(self._valid())
        runtime = parse_runtime(
            [],
            {
                "WORKSPACE_GUARD_MCP_ROOT": str(self.workspace),
                "WORKSPACE_GUARD_MCP_TASK_CONFIG": str(self.config_path),
            },
        )
        self.assertIsNotNone(runtime.task_configuration)
        self.assertIn("test", runtime.task_configuration.tasks)  # type: ignore[union-attr]

    def test_python_profiles_are_optional_strict_and_frozen(self) -> None:
        value = self._valid()
        value["profiles"] = {
            "debug": {
                "image": PINNED_IMAGE,
                "tools": [
                    "python_version",
                    "run_pytest",
                    "run_python_script",
                ],
                "workspace_access": "read-only",
            }
        }
        configuration = load_task_config(
            self._write(value), workspace_root=self.workspace
        )

        profile = configuration.profiles["debug"]
        self.assertEqual(profile.name, "debug")
        self.assertEqual(
            profile.tools,
            {"python_version", "run_pytest", "run_python_script"},
        )
        with self.assertRaises(TypeError):
            configuration.profiles["other"] = profile  # type: ignore[index]

    def test_default_profile_is_validated_and_frozen(self) -> None:
        value = self._valid()
        value["profiles"] = {
            "safe": {"image": PINNED_IMAGE, "tools": ["run_pytest"]},
            "coding": {
                "image": PINNED_IMAGE,
                "tools": ["run_pytest", "run_command"],
                "allow_arbitrary_commands": True,
            },
        }
        value["default_profile"] = "coding"
        configuration = load_task_config(
            self._write(value), workspace_root=self.workspace
        )
        self.assertEqual(configuration.default_profile, "coding")

        for invalid in ("missing", 1, True):
            with self.subTest(invalid=invalid):
                mutated = json.loads(json.dumps(value))
                mutated["default_profile"] = invalid
                with self.assertRaisesRegex(TaskConfigurationError, "default_profile"):
                    load_task_config(
                        self._write(mutated), workspace_root=self.workspace
                    )

    def test_generic_command_profile_requires_explicit_arbitrary_code_grant(
        self,
    ) -> None:
        value = self._valid()
        value["profiles"] = {
            "coding": {
                "image": "sha256:" + "c" * 64,
                "tools": ["run_command", "start_command"],
                "workspace_access": "read-only",
                "allow_arbitrary_commands": True,
            }
        }
        configuration = load_task_config(
            self._write(value), workspace_root=self.workspace
        )

        profile = configuration.profiles["coding"]
        self.assertEqual(profile.tools, {"run_command", "start_command"})
        self.assertTrue(profile.allow_arbitrary_commands)
        with self.assertRaises(TypeError):
            configuration.profiles["other"] = profile  # type: ignore[index]

        for grant in (None, False, "true", 1):
            with self.subTest(grant=grant):
                invalid = json.loads(json.dumps(value))
                profile_value = invalid["profiles"]["coding"]
                if grant is None:
                    profile_value.pop("allow_arbitrary_commands")
                else:
                    profile_value["allow_arbitrary_commands"] = grant
                with self.assertRaisesRegex(
                    TaskConfigurationError,
                    "allow_arbitrary_commands",
                ):
                    load_task_config(
                        self._write(invalid), workspace_root=self.workspace
                    )

        duplicate = json.loads(json.dumps(value))
        duplicate["profiles"]["coding"]["tools"].append("run_command")
        with self.assertRaisesRegex(TaskConfigurationError, "duplicate tool"):
            load_task_config(self._write(duplicate), workspace_root=self.workspace)

    def test_profile_only_configuration_and_invalid_profiles(self) -> None:
        base = {
            "version": 1,
            "runtime": "docker",
            "profiles": {
                "debug": {
                    "image": PINNED_IMAGE,
                    "tools": ["run_pytest"],
                }
            },
        }
        configuration = load_task_config(
            self._write(base), workspace_root=self.workspace
        )
        self.assertEqual(configuration.tasks, {})
        self.assertIn("debug", configuration.profiles)

        writable = json.loads(json.dumps(base))
        writable["profiles"]["debug"]["workspace_access"] = "writable"
        with self.assertRaisesRegex(TaskConfigurationError, "explicit limit"):
            load_task_config(self._write(writable), workspace_root=self.workspace)

        mutations = (
            ("image", "python:3.13", "sha256 digest"),
            ("tools", ["host_shell"], "unsupported execution tool"),
            ("workspace_access", "host", "workspace_access"),
            ("env", {"TOKEN": "secret"}, "unknown field"),
        )
        for field, invalid, message in mutations:
            with self.subTest(field=field):
                value = json.loads(json.dumps(base))
                profile = value["profiles"]["debug"]
                profile[field] = invalid
                with self.assertRaisesRegex(TaskConfigurationError, message):
                    load_task_config(self._write(value), workspace_root=self.workspace)

    def test_path_must_be_absolute_outside_workspace_and_not_a_symlink(self) -> None:
        self._write(self._valid())
        with self.assertRaisesRegex(TaskConfigurationError, "absolute"):
            load_task_config("tasks.json", workspace_root=self.workspace)

        inside = self.workspace / "tasks.json"
        contents = self.config_path.read_text(encoding="utf-8")
        inside.write_text(contents, encoding="utf-8")
        with self.assertRaisesRegex(TaskConfigurationError, "outside"):
            load_task_config(inside, workspace_root=self.workspace)

        link = self.base / "tasks-link.json"
        try:
            link.symlink_to(self.config_path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(TaskConfigurationError, "symbolic link"):
            load_task_config(link, workspace_root=self.workspace)

    @unittest.skipIf(os.name == "nt", "POSIX FIFO is not portable")
    def test_special_and_oversized_configs_are_rejected(self) -> None:
        fifo = self.base / "tasks.fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(TaskConfigurationError, "regular file"):
            load_task_config(fifo, workspace_root=self.workspace)

        self.config_path.write_bytes(b"{}" * 20)
        with self.assertRaisesRegex(TaskConfigurationError, "size limit"):
            load_task_config(
                self.config_path, workspace_root=self.workspace, max_bytes=10
            )

    def test_invalid_json_duplicate_and_unknown_fields_are_rejected(self) -> None:
        self.config_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaisesRegex(TaskConfigurationError, "invalid task config JSON"):
            load_task_config(self.config_path, workspace_root=self.workspace)

        self.config_path.write_text(
            '{"version":1,"version":1,"runtime":"docker","tasks":{}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(TaskConfigurationError, "duplicate JSON field"):
            load_task_config(self.config_path, workspace_root=self.workspace)

        value = self._valid()
        value["unexpected"] = True
        with self.assertRaisesRegex(TaskConfigurationError, "unknown field"):
            load_task_config(self._write(value), workspace_root=self.workspace)

    def test_task_names_modes_argv_and_images_are_strict(self) -> None:
        mutations = (
            ("invalid name", "tasks", "invalid task name"),
            ("bad_mode", "mode", "mode must"),
            ("empty_argv", "argv", "non-empty array"),
            ("floating_image", "image", "sha256 digest"),
            ("unknown_task_field", "field", "unknown field"),
        )
        for label, field, message in mutations:
            with self.subTest(case=label):
                value = self._valid()
                tasks = value["tasks"]
                assert isinstance(tasks, dict)
                task = tasks["test"]
                assert isinstance(task, dict)
                if field == "tasks":
                    tasks["bad name"] = tasks.pop("test")
                elif field == "mode":
                    task["mode"] = "shell"
                elif field == "argv":
                    task["argv"] = []
                elif field == "image":
                    task["image"] = "python:3.13-slim"
                else:
                    task["env"] = {"TOKEN": "secret"}
                with self.assertRaisesRegex(TaskConfigurationError, message):
                    load_task_config(self._write(value), workspace_root=self.workspace)

        short_id = self._valid()
        tasks = short_id["tasks"]
        assert isinstance(tasks, dict)
        task = tasks["test"]
        assert isinstance(task, dict)
        task["image"] = "sha256:abc123"
        with self.assertRaisesRegex(TaskConfigurationError, "full local sha256"):
            load_task_config(self._write(short_id), workspace_root=self.workspace)

    def test_invalid_limits_and_top_level_contract_are_rejected(self) -> None:
        cases = (
            ("runtime", "runc", "runtime"),
            ("version", 2, "version"),
            ("timeout_seconds", 0, "timeout_seconds"),
            ("max_output_bytes", True, "max_output_bytes"),
            ("max_snapshot_files", -1, "max_snapshot_files"),
            ("memory", "unlimited", "memory"),
            ("cpus", "nan", "cpus"),
            ("pids", 0, "pids"),
            ("max_concurrent_tasks", 1000, "max_concurrent_tasks"),
            ("unknown_limit", 1, "unknown field"),
        )
        for field, invalid, message in cases:
            with self.subTest(field=field):
                value = self._valid()
                if field in {"runtime", "version"}:
                    value[field] = invalid
                else:
                    limits = value["limits"]
                    assert isinstance(limits, dict)
                    limits[field] = invalid
                with self.assertRaisesRegex(TaskConfigurationError, message):
                    load_task_config(self._write(value), workspace_root=self.workspace)

    def test_cli_reports_configuration_error_before_startup(self) -> None:
        self.config_path.write_text("{}", encoding="utf-8")
        with (
            contextlib.redirect_stderr(io.StringIO()) as error,
            self.assertRaises(SystemExit),
        ):
            parse_runtime(
                [
                    "--root",
                    str(self.workspace),
                    "--task-config",
                    str(self.config_path),
                ],
                {},
            )
        self.assertIn("missing field", error.getvalue())


if __name__ == "__main__":
    unittest.main()
