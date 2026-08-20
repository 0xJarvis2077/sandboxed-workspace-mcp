from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ATTEMPTED = 20


def main() -> None:
    children: list[subprocess.Popen[bytes]] = []
    failure_type: str | None = None
    failure_errno: int | None = None
    failure_errno_name: str | None = None
    limits_text = ""
    limits_path = Path("/proc/self/limits")
    if limits_path.exists():
        limits_text = limits_path.read_text(encoding="utf-8", errors="replace")
    max_processes = next(
        (line for line in limits_text.splitlines() if line.startswith("Max processes")),
        None,
    )
    try:
        for _ in range(ATTEMPTED):
            try:
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(1.5)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                failure_type = type(exc).__name__
                failure_errno = exc.errno
                failure_errno_name = (
                    None if exc.errno is None else errno.errorcode.get(exc.errno)
                )
                break
            children.append(child)
        print(
            json.dumps(
                {
                    "attempted": ATTEMPTED,
                    "started": len(children),
                    "failure_type": failure_type,
                    "failure_errno": failure_errno,
                    "failure_errno_name": failure_errno_name,
                    "uid": os.getuid(),
                    "euid": os.geteuid(),
                    "max_processes": max_processes,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        deadline = time.monotonic() + 2.0
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                child.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1.0)


if __name__ == "__main__":
    main()
