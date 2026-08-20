from __future__ import annotations

import json
import sys
from pathlib import Path

PRIVATE_SENTINEL = Path("/tmp/round75-isolation-sentinel")


def main() -> None:
    mode = sys.argv[1]
    artifacts = Path("/artifacts")
    if mode == "write":
        PRIVATE_SENTINEL.write_text("execution-a\n", encoding="utf-8")
        (artifacts / "execution-a.txt").write_text("execution-a\n", encoding="utf-8")
        print(json.dumps({"written": True}), flush=True)
        return
    if mode != "inspect":
        raise SystemExit(2)
    print(
        json.dumps(
            {
                "private_sentinel_visible": PRIVATE_SENTINEL.exists(),
                "artifact_names": sorted(path.name for path in artifacts.iterdir()),
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
