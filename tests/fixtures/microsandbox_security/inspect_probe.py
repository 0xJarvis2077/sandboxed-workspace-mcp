from __future__ import annotations

import json
import os
from pathlib import Path

CAP_SYS_ADMIN = 21
SELECTED_ENV = (
    "WORKSPACE_GUARD_SECRET_SENTINEL",
    "AWS_SECRET_ACCESS_KEY",
    "OPENAI_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "SSH_AUTH_SOCK",
)


def _proc_status() -> dict[str, str]:
    result: dict[str, str] = {}
    path = Path("/proc/self/status")
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key] = value.strip()
    return result


def _mount_options(mountpoint: str) -> list[str] | None:
    path = Path("/proc/self/mountinfo")
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[4] != mountpoint or "-" not in fields:
            continue
        separator = fields.index("-")
        options = set(fields[5].split(","))
        if separator + 3 < len(fields):
            options.update(fields[separator + 3].split(","))
        return sorted(options)
    return None


def main() -> None:
    status = _proc_status()
    cap_eff_raw = status.get("CapEff")
    cap_eff = int(cap_eff_raw, 16) if cap_eff_raw else None
    affinity_count = None
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        affinity_count = len(sched_getaffinity(0))

    rootfs_sentinel = Path("/round75-rootfs-sentinel")
    rootfs_write_success = False
    try:
        rootfs_sentinel.write_text("private-rootfs\n", encoding="utf-8")
        rootfs_write_success = True
    except OSError:
        pass

    print(
        json.dumps(
            {
                "no_new_privs": status.get("NoNewPrivs"),
                "cap_eff": cap_eff_raw,
                "cap_sys_admin": (
                    None if cap_eff is None else bool(cap_eff & (1 << CAP_SYS_ADMIN))
                ),
                "cpu_count": os.cpu_count(),
                "affinity_count": affinity_count,
                "workspace_mount_options": _mount_options("/workspace"),
                "artifact_mount_options": _mount_options("/artifacts"),
                "selected_env": {name: os.environ.get(name) for name in SELECTED_ENV},
                "rootfs_write_success": rootfs_write_success,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
