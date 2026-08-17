"""Canonical repository quality gate for local development and CI."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _run(
    *args: str,
    environment: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> None:
    command = [PYTHON, *args]
    print(f"+ {' '.join(command)}", flush=True)
    env = os.environ.copy()
    if environment is not None:
        env.update(environment)
    subprocess.run(command, cwd=cwd, check=True, env=env)


def run_fast_checks() -> None:
    """Run the inexpensive checks used by the pre-push hook."""

    _run("-m", "ruff", "check", ".")
    _run("-m", "ruff", "format", "--check", ".")
    _run("-m", "mypy", "src", "tests")


def _copy_build_source(destination: Path) -> None:
    shutil.copytree(
        ROOT,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".coverage",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "*.egg-info",
            "build",
            "dist",
        ),
    )


def run_full_checks() -> None:
    """Run the complete quality gate used by CI."""

    run_fast_checks()
    with tempfile.TemporaryDirectory(prefix="workspace-guard-mcp-check-") as temp:
        temp_dir = Path(temp)
        environment = {
            "COVERAGE_FILE": str(temp_dir / ".coverage"),
            "PYTHONPYCACHEPREFIX": str(temp_dir / "pycache"),
        }
        _run(
            "-m",
            "coverage",
            "run",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
            environment=environment,
        )
        _run(
            "-m",
            "coverage",
            "report",
            "--fail-under=85",
            environment=environment,
        )
        _run(
            "-m",
            "compileall",
            "-q",
            "server.py",
            "src",
            "tests",
            "scripts",
            environment=environment,
        )

        source_dir = temp_dir / "source"
        build_dir = temp_dir / "dist"
        _copy_build_source(source_dir)
        _run("-m", "build", "--outdir", str(build_dir), cwd=source_dir)
        wheels = sorted(build_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(
                f"expected exactly one wheel from build, found {len(wheels)}"
            )
        _run("scripts/wheel_smoke.py", str(wheels[0]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="run Ruff lint/format checks and mypy only",
    )
    args = parser.parse_args()

    if args.fast:
        run_fast_checks()
    else:
        run_full_checks()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
