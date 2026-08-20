from __future__ import annotations

import json
from pathlib import Path

TARGET_BYTES = 96 * 1024


def main() -> None:
    path = Path("/workspace/fsize.bin")
    failure_type: str | None = None
    failure_errno: int | None = None
    bytes_written: int | None = None
    try:
        with path.open("wb", buffering=0) as handle:
            bytes_written = handle.write(b"x" * TARGET_BYTES)
    except OSError as exc:
        failure_type = type(exc).__name__
        failure_errno = exc.errno
    size = path.stat().st_size if path.exists() else 0
    print(
        json.dumps(
            {
                "target_bytes": TARGET_BYTES,
                "bytes_written": bytes_written,
                "size_bytes": size,
                "failure_type": failure_type,
                "failure_errno": failure_errno,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
