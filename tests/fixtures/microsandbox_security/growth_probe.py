from __future__ import annotations

import json
import time
from pathlib import Path


def main() -> None:
    root = Path("/workspace/generated")
    root.mkdir(exist_ok=True)
    created = 0
    for index in range(16):
        (root / f"chunk-{index:02d}.bin").write_bytes(b"x" * (16 * 1024))
        created += 1
        time.sleep(0.03)
    print(json.dumps({"created": created}, sort_keys=True), flush=True)
    time.sleep(2.0)


if __name__ == "__main__":
    main()
