"""Read-only Git operations with bounded output and no user-controlled arguments."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from typing import BinaryIO

from .access_policy import AccessPolicy
from .config import Settings
from .workspace import Workspace, truncate_utf8


class GitError(RuntimeError):
    """Raised when a read-only Git operation cannot be completed safely."""


class GitReader:
    """Execute a closed set of read-only Git queries inside a workspace."""

    def __init__(self, settings: Settings, executable: str | None = None) -> None:
        self.settings = settings
        self.root = settings.root
        self.executable = executable or shutil.which("git")
        self.policy = AccessPolicy(settings.blocked_patterns)
        self.workspace = Workspace(settings)

    def status(self, style: str = "default") -> str:
        if style not in {"default", "short", "porcelain"}:
            raise GitError("git status style must be default, short, or porcelain")
        args = ["status"]
        if style == "default":
            args.extend(["--short", "--branch"])
        elif style == "short":
            args.append("--short")
        else:
            args.append("--porcelain=v1")
        args.extend(
            [
                "--untracked-files=normal",
                "--ignore-submodules=all",
                "--",
                ".",
                *self.policy.git_exclude_pathspecs(),
            ]
        )
        return self._run(args)

    def diff(self, *, staged: bool = False, path: str | None = None) -> str:
        args = [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--ignore-submodules=all",
        ]
        if staged:
            args.append("--cached")
        args.extend(["--", *self._pathspecs(path)])
        return self._run(args)

    def log(self, count: int = 10, *, oneline: bool = False) -> str:
        if type(count) is not int or not 1 <= count <= 50:
            raise GitError("git log count must be an integer between 1 and 50")
        # The stable one-line format remains the only output format. ``oneline`` is
        # an explicit compatibility selector, not a switch to a second renderer.
        if type(oneline) is not bool:
            raise GitError("git log oneline must be a boolean")
        return self._run(
            [
                "log",
                f"-{count}",
                "--oneline",
                "--decorate",
                "--no-show-signature",
            ]
        )

    def show(self, commit: str, *, path: str | None = None) -> str:
        if (
            not isinstance(commit, str)
            or re.fullmatch(r"(?:HEAD|[0-9a-fA-F]{7,40})", commit) is None
        ):
            raise GitError(
                "git show commit must be HEAD or a 7-40 character hexadecimal ID"
            )
        commit_object = f"{commit}^{{commit}}"
        return self._run(
            [
                "show",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-show-signature",
                commit_object,
                "--",
                *self._pathspecs(path),
            ]
        )

    def branches(self, *, show_current: bool = False) -> str:
        if type(show_current) is not bool:
            raise GitError("git branch show_current must be a boolean")
        if show_current:
            return self._run(["branch", "--show-current", "--no-color"])
        return self._run(["branch", "--list", "--no-color"])

    def rev_parse(self, query: str) -> str:
        allowed_queries = {
            "--is-inside-work-tree",
            "--show-prefix",
            "--show-toplevel",
            "HEAD",
        }
        if query not in allowed_queries:
            raise GitError(f"git rev-parse query is not allowed: {query}")
        return self._run(["rev-parse", query])

    def ls_files(self) -> str:
        return self._run(["ls-files", "--", ".", *self.policy.git_exclude_pathspecs()])

    def _pathspecs(self, path: str | None) -> list[str]:
        if path is None:
            return [".", *self.policy.git_exclude_pathspecs()]
        if not isinstance(path, str) or not path:
            raise GitError("git path must be a non-empty string")
        target = self.workspace.safe_path(path)
        relative = self.workspace.relative_path(target)
        return [f":(literal){relative}"]

    def _run(self, args: list[str]) -> str:
        if not self.executable:
            raise GitError("git executable was not found")

        command = [
            self.executable,
            "--no-pager",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            f"core.excludesFile={os.devnull}",
            "-c",
            "diff.external=",
            "-c",
            "diff.trustExitCode=false",
            *args,
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as exc:
            raise GitError(f"failed to start git executable: {exc}") from exc

        if process.stdout is None or process.stderr is None:  # pragma: no cover
            process.kill()
            raise GitError("failed to capture git output")

        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()
        output_lock = threading.Lock()

        def consume_output(stream: BinaryIO, destination: bytearray) -> None:
            try:
                while chunk := stream.read(64 * 1024):
                    with output_lock:
                        remaining = (
                            self.settings.max_output_size
                            + 1
                            - len(stdout)
                            - len(stderr)
                        )
                        if remaining > 0:
                            destination.extend(chunk[:remaining])
                        if len(chunk) > remaining or (
                            len(stdout) + len(stderr) > self.settings.max_output_size
                        ):
                            overflow.set()
                    if overflow.is_set():
                        try:
                            process.terminate()
                        except OSError:
                            pass
                        return
            except OSError:
                return

        readers = [
            threading.Thread(
                target=consume_output,
                args=(process.stdout, stdout),
                daemon=True,
            ),
            threading.Thread(
                target=consume_output,
                args=(process.stderr, stderr),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            process.wait(timeout=self.settings.git_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        finally:
            for reader in readers:
                reader.join(timeout=1.0)
            process.stdout.close()
            process.stderr.close()

        diagnostic = self._diagnostic(stdout, stderr)
        if overflow.is_set():
            raise GitError(
                self._bounded_error(
                    f"git output exceeded {self.settings.max_output_size} bytes",
                    diagnostic,
                )
            )
        if timed_out:
            raise GitError(
                self._bounded_error(
                    "git command timed out after "
                    f"{self.settings.git_timeout:g} seconds",
                    diagnostic,
                )
            )
        if process.returncode:
            raise GitError(
                self._bounded_error(
                    f"git command failed with exit code {process.returncode}",
                    diagnostic,
                )
            )

        text = bytes(stdout).decode("utf-8", errors="replace")
        if stderr:
            text += ("\n" if text else "") + bytes(stderr).decode(
                "utf-8", errors="replace"
            )
        return truncate_utf8(text or "(no output)", self.settings.max_output_size)

    def _diagnostic(self, stdout: bytearray, stderr: bytearray) -> str:
        sections: list[str] = []
        if stderr:
            sections.append(
                "stderr: " + bytes(stderr).decode("utf-8", errors="replace").strip()
            )
        if stdout:
            sections.append(
                "stdout: " + bytes(stdout).decode("utf-8", errors="replace").strip()
            )
        return truncate_utf8("\n".join(sections), self.settings.max_output_size)

    def _bounded_error(self, message: str, diagnostic: str) -> str:
        if diagnostic:
            message = f"{message}\n{diagnostic}"
        return truncate_utf8(message, self.settings.max_output_size)

    def _environment(self) -> dict[str, str]:
        safe_tempdir = "/tmp" if os.name != "nt" else str(self.root)
        environment = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": os.devnull,
            "GIT_SEQUENCE_EDITOR": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "HOME": str(self.root),
            "LANG": "C",
            "LC_ALL": "C",
            "PAGER": "cat",
            "PATH": os.defpath,
            "TMPDIR": safe_tempdir,
            "TEMP": safe_tempdir,
            "TMP": safe_tempdir,
        }
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            environment["USERPROFILE"] = str(self.root)
        return environment
