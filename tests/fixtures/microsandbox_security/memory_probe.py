from __future__ import annotations

import json

CHUNK_MIB = 8
TARGET_MIB = 512


def main() -> None:
    chunks: list[bytearray] = []
    failure_type: str | None = None
    allocated_mib = 0
    try:
        while allocated_mib < TARGET_MIB:
            chunks.append(bytearray(CHUNK_MIB * 1024 * 1024))
            allocated_mib += CHUNK_MIB
    except (MemoryError, OSError) as exc:
        failure_type = type(exc).__name__
    print(
        json.dumps(
            {
                "allocated_mib": allocated_mib,
                "target_mib": TARGET_MIB,
                "failure_type": failure_type,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
