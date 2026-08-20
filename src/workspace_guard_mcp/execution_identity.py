"""Shared local execution identity policy for sandbox backends."""

from __future__ import annotations

import os

_FALLBACK_UID = 65532
_FALLBACK_GID = 65532


def local_execution_user() -> str:
    """Return a non-root numeric UID:GID suitable for isolated guest execution."""

    get_uid = getattr(os, "getuid", None)
    get_gid = getattr(os, "getgid", None)
    if get_uid is not None and get_gid is not None:
        uid = get_uid()
        gid = get_gid()
        if uid > 0:
            return f"{uid}:{gid}"
    return f"{_FALLBACK_UID}:{_FALLBACK_GID}"
