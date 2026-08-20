from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> None:
    artifacts = Path("/artifacts")
    workspace = Path("/workspace")
    outcomes: dict[str, object] = {}

    (artifacts / "allowed.txt").write_text("allowed\n", encoding="utf-8")

    try:
        (artifacts / "directory").mkdir()
        outcomes["directory"] = True
    except OSError:
        outcomes["directory"] = False

    try:
        (artifacts / "symlink").symlink_to("allowed.txt")
        outcomes["symlink"] = True
    except OSError:
        outcomes["symlink"] = False

    fifo = artifacts / "fifo"
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is not None:
        try:
            mkfifo(fifo)
            outcomes["fifo"] = True
        except OSError:
            outcomes["fifo"] = False
    else:
        outcomes["fifo"] = None

    traversal = artifacts / ".." / "workspace" / "artifact-traversal.txt"
    try:
        traversal.write_text("escape\n", encoding="utf-8")
        outcomes["traversal_write"] = True
    except OSError:
        outcomes["traversal_write"] = False

    outcomes["workspace_traversal_exists"] = (
        workspace / "artifact-traversal.txt"
    ).exists()
    print(json.dumps(outcomes, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
