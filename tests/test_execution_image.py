from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workspace_guard_mcp.task_config import load_task_config


class ExecutionImageCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_root = Path(__file__).resolve().parents[1]
        self.dockerfile = (
            self.project_root / "examples" / "Dockerfile.task"
        ).read_text(encoding="utf-8")
        self.ci = (self.project_root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

    def test_standard_image_smoke_check_covers_declared_analysis_capabilities(
        self,
    ) -> None:
        for dependency in ("coverage[toml]", "pytest", "mypy", "ruff"):
            with self.subTest(dependency=dependency):
                self.assertIn(f'"{dependency}', self.dockerfile)

        self.assertIn("FROM python:", self.dockerfile)
        for command in (
            "python --version",
            "python -m pytest --version",
            "python -m coverage --version",
            "python -m mypy --version",
            "ruff --version",
        ):
            with self.subTest(command=command):
                self.assertIn(command, self.dockerfile)
        self.assertIn("import coverage, mypy, pytest", self.dockerfile)
        self.assertIn("shutil.which('ruff')", self.dockerfile)

    def test_profiles_with_structured_analysis_use_complete_capability_set(
        self,
    ) -> None:
        payload = json.loads(
            (self.project_root / "examples" / "execution-profiles.json").read_text(
                encoding="utf-8"
            )
        )
        profiles = payload["profiles"]
        profiles["python-safe"]["image"] = "sha256:" + "a" * 64
        required = {
            "run_pytest",
            "run_ruff",
            "run_mypy",
            "run_pytest_coverage",
        }

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            workspace.mkdir()
            config_path = base / "execution-profiles.json"
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            configuration = load_task_config(config_path, workspace_root=workspace)

        for profile_name in ("python-safe", "coding"):
            with self.subTest(profile=profile_name):
                self.assertTrue(
                    required.issubset(configuration.profiles[profile_name].tools)
                )

    def test_ci_builds_and_offline_smoke_checks_execution_image(self) -> None:
        self.assertIn("execution-image:", self.ci)
        self.assertIn("docker build", self.ci)
        self.assertIn("examples/Dockerfile.task", self.ci)
        self.assertIn("docker run --rm --network none", self.ci)
        self.assertIn("python -m mypy --version", self.ci)


if __name__ == "__main__":
    unittest.main()
