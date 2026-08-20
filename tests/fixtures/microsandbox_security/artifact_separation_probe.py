from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    artifact = Path("/artifacts/allowed.txt")
    artifact.write_text("allowed\n", encoding="utf-8")
    workspace_target = Path("/artifacts/../workspace/artifact-traversal.txt")
    traversal_write = False
    try:
        workspace_target.write_text("escape\n", encoding="utf-8")
        traversal_write = True
    except OSError:
        pass
    print(
        json.dumps(
            {
                "artifact_write": artifact.exists(),
                "traversal_write": traversal_write,
                "workspace_target_exists": Path(
                    "/workspace/artifact-traversal.txt"
                ).exists(),
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
