"""Apply safe repository-wide formatting and lint fixes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _run(*args: str) -> None:
    command = [PYTHON, *args]
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    _run("-m", "ruff", "check", ".", "--fix")
    _run("-m", "ruff", "format", ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
