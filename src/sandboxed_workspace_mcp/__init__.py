"""A sandboxed local-workspace MCP server."""

__version__ = "0.2.0"

from .config import Settings
from .server import create_server

__all__ = ["Settings", "create_server"]
