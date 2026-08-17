"""Install the repository-managed Git hooks into the local checkout."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "pre-push"


def _hooks_directory() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    destination = _hooks_directory() / "pre-push"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
    print(f"installed pre-push hook: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
