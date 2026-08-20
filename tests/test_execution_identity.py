from __future__ import annotations

import unittest
from unittest.mock import patch

from workspace_guard_mcp.execution_identity import local_execution_user


class ExecutionIdentityTests(unittest.TestCase):
    def test_uses_non_root_host_uid_and_gid(self) -> None:
        with (
            patch("workspace_guard_mcp.execution_identity.os.getuid", return_value=501),
            patch("workspace_guard_mcp.execution_identity.os.getgid", return_value=20),
        ):
            self.assertEqual(local_execution_user(), "501:20")

    def test_root_host_falls_back_to_fixed_non_root_identity(self) -> None:
        with (
            patch("workspace_guard_mcp.execution_identity.os.getuid", return_value=0),
            patch("workspace_guard_mcp.execution_identity.os.getgid", return_value=0),
        ):
            self.assertEqual(local_execution_user(), "65532:65532")


if __name__ == "__main__":
    unittest.main()
