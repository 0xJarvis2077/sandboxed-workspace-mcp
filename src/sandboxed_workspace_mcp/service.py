"""Application service that coordinates workspace and Git capabilities."""

from __future__ import annotations

import re
import shlex

from .config import Settings
from .git_reader import GitReader
from .trash import TrashManager
from .workspace import Workspace


class CommandError(ValueError):
    """Raised when a shell-like command is outside the explicit grammar."""


class SandboxedWorkspace:
    """Facade exposed by the MCP adapter."""

    def __init__(self, settings: Settings, *, git: GitReader | None = None) -> None:
        self.settings = settings
        self.workspace = Workspace(settings)
        self.git = git or GitReader(settings)
        self.trash = TrashManager(self.workspace)

    def run_shell(self, command: str) -> str:
        """Parse a tiny shell-like grammar without invoking a real shell."""

        if not command or not command.strip():
            raise CommandError("empty command")
        if len(command) > 10_000:
            raise CommandError("command is too long")
        self._reject_shell_operators(command)

        try:
            args = shlex.split(command)
        except ValueError as exc:
            raise CommandError(f"cannot parse command: {exc}") from exc
        if not args:
            raise CommandError("empty command")

        program = args[0]
        if program == "pwd":
            self._require_arity(args, 1, "pwd")
            return str(self.settings.root)

        if program == "ls":
            return self._run_ls(args)

        if program == "cat":
            self._require_arity(args, 2, "cat <file>")
            return self.workspace.read_file(args[1])

        if program == "head":
            path, count = self._file_and_line_count(args, "head")
            return self.workspace.read_file(path, start_line=1, end_line=count)

        if program == "tail":
            path, count = self._file_and_line_count(args, "tail")
            return self.workspace.tail_file(path, count)

        if program == "tree":
            return self._run_tree(args)

        if program == "grep":
            self._require_arity(args, 3, "grep <text> <file>")
            return self.workspace.grep_file(args[1], args[2])

        if program == "rg":
            return self._run_rg(args)

        if program == "find":
            return self._run_find(args)

        if program == "wc":
            return self._run_wc(args)

        if program == "sed":
            return self._run_sed(args)

        if program == "git":
            return self._run_git(args)

        raise CommandError(
            f"command is not allowed: {program}; allowed commands are "
            "pwd, ls, cat, head, tail, tree, grep, rg, find, wc, sed, and "
            "policy-bounded git queries"
        )

    def _run_ls(self, args: list[str]) -> str:
        path = "."
        path_seen = False
        options_done = False
        for token in args[1:]:
            if token == "--" and not options_done:
                options_done = True
                continue
            if not options_done and token.startswith("-"):
                if token in {"--all", "--human-readable"}:
                    continue
                if not re.fullmatch(r"-[alh1]+", token):
                    raise CommandError(f"ls option is not allowed: {token}")
                continue
            if path_seen:
                raise CommandError("usage: ls [-alh1] [path]")
            path = token
            path_seen = True
        return self.workspace.list_directory(path)

    def _run_tree(self, args: list[str]) -> str:
        path = "."
        depth = 4
        remaining = args[1:]
        if len(remaining) >= 2 and remaining[0] in {"-L", "--max-depth"}:
            depth = self._line_count(remaining[1])
            remaining = remaining[2:]
        if len(remaining) > 1:
            raise CommandError("usage: tree [-L depth] [path]")
        if remaining:
            path = remaining[0]
        return self.workspace.tree(path, max_depth=depth)

    def _run_rg(self, args: list[str]) -> str:
        files_only = False
        ignore_case = False
        smart_case = False
        fixed_strings = False
        line_numbers = False
        path_glob: str | None = None
        positional: list[str] = []
        options_done = False
        index = 1
        while index < len(args):
            token = args[index]
            if token == "--" and not options_done:
                options_done = True
                index += 1
                continue
            if not options_done and token.startswith("-"):
                if token == "--files":
                    files_only = True
                    index += 1
                    continue
                if token in {"-g", "--glob"}:
                    if path_glob is not None:
                        raise CommandError("rg accepts at most one -g glob")
                    if index + 1 >= len(args):
                        raise CommandError("rg -g requires a glob argument")
                    path_glob = args[index + 1]
                    index += 2
                    continue
                if token == "--line-number":
                    line_numbers = True
                    index += 1
                    continue
                if token == "--fixed-strings":
                    fixed_strings = True
                    index += 1
                    continue
                if token == "--ignore-case":
                    ignore_case = True
                    index += 1
                    continue
                if token == "--smart-case":
                    smart_case = True
                    index += 1
                    continue
                if re.fullmatch(r"-[niFS]+", token):
                    ignore_case = ignore_case or "i" in token
                    smart_case = smart_case or "S" in token
                    fixed_strings = fixed_strings or "F" in token
                    line_numbers = line_numbers or "n" in token
                    index += 1
                    continue
                raise CommandError(f"rg option is not allowed: {token}")
            positional.append(token)
            index += 1

        if files_only:
            if ignore_case or smart_case or fixed_strings or line_numbers:
                raise CommandError(
                    "rg --files only accepts -g GLOB and an optional path"
                )
            if len(positional) > 1:
                raise CommandError("usage: rg --files [-g GLOB] [path]")
            return self.workspace.find_paths(
                positional[0] if positional else ".",
                kind="file",
                path_glob=path_glob,
            )

        if not 1 <= len(positional) <= 2:
            raise CommandError(
                "usage: rg [-niSF] [-g GLOB] <pattern> [path] | "
                "rg --files [-g GLOB] [path]"
            )
        pattern = positional[0]
        if not pattern:
            raise CommandError("rg pattern cannot be empty")
        effective_ignore_case = ignore_case or (
            smart_case and not any(character.isupper() for character in pattern)
        )
        return self.workspace.search_pattern(
            pattern,
            positional[1] if len(positional) == 2 else ".",
            fixed_strings=fixed_strings,
            ignore_case=effective_ignore_case,
            line_numbers=line_numbers,
            path_glob=path_glob,
        )

    def _run_find(self, args: list[str]) -> str:
        remaining = args[1:]
        path = "."
        if remaining and not remaining[0].startswith("-"):
            path = remaining.pop(0)

        max_depth: int | None = None
        kind: str | None = None
        name: str | None = None
        index = 0
        while index < len(remaining):
            option = remaining[index]
            if option not in {"-maxdepth", "-type", "-name"} or index + 1 >= len(
                remaining
            ):
                raise CommandError(
                    "usage: find [path] [-maxdepth N] [-type f|d] [-name glob]"
                )
            value = remaining[index + 1]
            if option == "-maxdepth":
                max_depth = self._nonnegative_int(value, "find maxdepth")
            elif option == "-type":
                if value not in {"f", "d"}:
                    raise CommandError("find type must be f or d")
                kind = "file" if value == "f" else "directory"
            else:
                if not value:
                    raise CommandError("find name glob cannot be empty")
                name = value
            index += 2

        return self.workspace.find_paths(
            path, max_depth=max_depth, kind=kind, name=name
        )

    def _run_wc(self, args: list[str]) -> str:
        self._require_arity(args, 3, "wc -l|-w|-c <file>")
        metrics = {"-l": "lines", "-w": "words", "-c": "bytes"}
        metric = metrics.get(args[1])
        if metric is None:
            raise CommandError("wc only accepts one of -l, -w, or -c")
        return f"{self.workspace.count_file(args[2], metric)} {args[2]}"

    def _run_sed(self, args: list[str]) -> str:
        self._require_arity(args, 4, "sed -n '<start>[,<end>]p' <file>")
        if args[1] != "-n":
            raise CommandError("sed only accepts -n range printing")
        match = re.fullmatch(r"([1-9][0-9]*)(?:,([1-9][0-9]*))?p", args[2])
        if match is None:
            raise CommandError("sed range must look like 5p or 5,20p")
        start = self._bounded_line_number(match.group(1))
        end = self._bounded_line_number(match.group(2) or match.group(1))
        if end < start:
            raise CommandError("sed range end is before its start")
        return self.workspace.read_file(args[3], start_line=start, end_line=end)

    def _run_git(self, args: list[str]) -> str:
        if len(args) < 2:
            raise CommandError(
                "usage: git status|diff|log|show|branch|rev-parse|ls-files"
            )

        subcommand = args[1]
        if subcommand == "status":
            self._require_range(args, 2, 3, "git status [--short|--porcelain]")
            if len(args) == 2:
                return self.git.status()
            styles = {"--short": "short", "--porcelain": "porcelain"}
            style = styles.get(args[2])
            if style is None:
                raise CommandError("git status only accepts --short or --porcelain")
            return self.git.status(style)

        if subcommand == "diff":
            remaining = args[2:]
            staged = False
            if remaining and remaining[0] in {"--cached", "--staged"}:
                staged = True
                remaining = remaining[1:]
            path: str | None = None
            if remaining:
                if len(remaining) != 2 or remaining[0] != "--":
                    raise CommandError("usage: git diff [--cached|--staged] [-- FILE]")
                path = remaining[1]
            return self.git.diff(staged=staged, path=path)

        if subcommand == "log":
            count = 10
            oneline = False
            remaining = args[2:]
            if remaining and remaining[0] == "--oneline":
                oneline = True
                remaining = remaining[1:]
            if remaining:
                if len(remaining) == 2 and remaining[0] == "-n":
                    count_text = remaining[1]
                elif len(remaining) == 1 and re.fullmatch(
                    r"-[1-9][0-9]?", remaining[0]
                ):
                    count_text = remaining[0][1:]
                else:
                    raise CommandError("usage: git log [--oneline] [-n N]")
                if not re.fullmatch(r"[1-9][0-9]?", count_text):
                    raise CommandError("git log count must be between 1 and 50")
                count = int(count_text)
                if count > 50:
                    raise CommandError("git log count must be at most 50")
            return self.git.log(count, oneline=oneline)

        if subcommand == "show":
            if len(args) == 3:
                return self.git.show(args[2])
            if len(args) == 5 and args[3] == "--":
                return self.git.show(args[2], path=args[4])
            raise CommandError("usage: git show COMMIT [-- FILE]")

        if subcommand == "branch":
            self._require_range(args, 2, 3, "git branch [--show-current]")
            if len(args) == 3 and args[2] != "--show-current":
                raise CommandError("git branch only accepts --show-current")
            return self.git.branches(show_current=len(args) == 3)

        if subcommand == "rev-parse":
            self._require_arity(args, 3, "git rev-parse <safe-query>")
            allowed_queries = {
                "--is-inside-work-tree",
                "--show-prefix",
                "--show-toplevel",
                "HEAD",
            }
            if args[2] not in allowed_queries:
                raise CommandError(f"git rev-parse query is not allowed: {args[2]}")
            return self.git.rev_parse(args[2])

        if subcommand == "ls-files":
            self._require_arity(args, 2, "git ls-files")
            return self.git.ls_files()

        raise CommandError(f"git command is not allowed: {subcommand}")

    @staticmethod
    def _line_count(value: str) -> int:
        try:
            count = int(value)
        except ValueError as exc:
            raise CommandError("line count must be an integer") from exc
        return max(1, min(count, 1_000))

    @staticmethod
    def _nonnegative_int(value: str, label: str) -> int:
        if not re.fullmatch(r"[0-9]+", value):
            raise CommandError(f"{label} must be a non-negative integer")
        return min(int(value), 1_000)

    @staticmethod
    def _bounded_line_number(value: str) -> int:
        number = int(value)
        if number > 1_000_000:
            raise CommandError("line number must be at most 1000000")
        return number

    def _file_and_line_count(self, args: list[str], program: str) -> tuple[str, int]:
        usage = f"{program} [-n lines] <file> | {program} <file> [lines]"
        if len(args) == 2:
            return args[1], 50
        if len(args) == 3 and args[1] != "-n":
            return args[1], self._line_count(args[2])
        if len(args) == 4 and args[1] == "-n":
            return args[3], self._line_count(args[2])
        raise CommandError(f"usage: {usage}")

    @staticmethod
    def _require_arity(args: list[str], expected: int, usage: str) -> None:
        if len(args) != expected:
            raise CommandError(f"usage: {usage}")

    @staticmethod
    def _require_range(args: list[str], minimum: int, maximum: int, usage: str) -> None:
        if not minimum <= len(args) <= maximum:
            raise CommandError(f"usage: {usage}")

    @staticmethod
    def _reject_shell_operators(command: str) -> None:
        """Reject unquoted shell control syntax before the closed grammar parses."""

        quote: str | None = None
        escaped = False
        index = 0
        while index < len(command):
            character = command[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if character == "\\" and quote != "'":
                escaped = True
                index += 1
                continue
            if character in {"'", '"'}:
                if quote is None:
                    quote = character
                elif quote == character:
                    quote = None
                index += 1
                continue
            if character == "`" or command.startswith("$(", index):
                raise CommandError("shell command substitution is not allowed")
            if quote is None and character in ";|&<>":
                raise CommandError("shell operators and redirection are not allowed")
            index += 1
