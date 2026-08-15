"""Backward-compatible source-tree entry point.

Prefer the installed ``sandboxed-workspace-mcp`` command. This wrapper keeps
``python server.py`` working without mutating ``sys.path``.
"""

from src.sandboxed_workspace_mcp.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
