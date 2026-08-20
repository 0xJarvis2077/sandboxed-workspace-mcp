from __future__ import annotations

import json
from pathlib import Path


def _attempt(name: str, action: object) -> dict[str, object]:
    try:
        callable_action = action
        assert callable(callable_action)
        callable_action()
    except OSError as exc:
        return {"success": False, "errno": exc.errno}
    return {"success": True, "errno": None}


def main() -> None:
    root = Path("/workspace")
    original = root / "original.txt"
    renamed = root / "renamed.txt"
    created = root / "created.txt"
    directory = root / "created-dir"

    results = {
        "create": _attempt(
            "create", lambda: created.write_text("created", encoding="utf-8")
        ),
        "overwrite": _attempt(
            "overwrite", lambda: original.write_text("changed", encoding="utf-8")
        ),
        "truncate": _attempt("truncate", lambda: original.open("w").close()),
        "rename": _attempt("rename", lambda: original.rename(renamed)),
        "unlink": _attempt("unlink", lambda: original.unlink()),
        "mkdir": _attempt("mkdir", lambda: directory.mkdir()),
        "chmod": _attempt("chmod", lambda: original.chmod(0o777)),
    }
    print(json.dumps(results, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
