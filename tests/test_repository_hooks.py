from __future__ import annotations

import unittest
from pathlib import Path


class RepositoryHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_root = Path(__file__).resolve().parents[1]
        self.pre_push = (self.project_root / "scripts" / "pre-push").read_text(
            encoding="utf-8"
        )
        self.install_hooks = (
            self.project_root / "scripts" / "install_hooks.py"
        ).read_text(encoding="utf-8")

    def test_pre_push_runs_from_repository_root_and_prefers_local_venv(self) -> None:
        self.assertIn("ROOT=$(git rev-parse --show-toplevel)", self.pre_push)
        self.assertIn('cd "$ROOT"', self.pre_push)
        self.assertLess(
            self.pre_push.index("$ROOT/.venv/bin/python"),
            self.pre_push.index("command -v python3"),
        )
        self.assertLess(
            self.pre_push.index("elif command -v python3 >/dev/null 2>&1; then"),
            self.pre_push.index("elif command -v python >/dev/null 2>&1; then"),
        )
        self.assertIn('exec "$PYTHON" "$ROOT/scripts/check.py" --fast', self.pre_push)

    def test_hook_installer_copies_managed_hook_and_marks_it_executable(self) -> None:
        self.assertIn('SOURCE = ROOT / "scripts" / "pre-push"', self.install_hooks)
        self.assertIn("shutil.copyfile(SOURCE, destination)", self.install_hooks)
        self.assertIn("stat.S_IXUSR", self.install_hooks)


if __name__ == "__main__":
    unittest.main()
