"""A bounded Thompson-NFA regex subset for untrusted workspace searches."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_PATTERN_CHARACTERS = 1024
MAX_PATTERN_BYTES = 4096
MAX_NFA_STATES = 4096


class SafeRegexError(ValueError):
    """Raised when a pattern is invalid or outside the safe regex subset."""


@dataclass(frozen=True, slots=True)
class _CharacterSet:
    literals: frozenset[str]
    ranges: tuple[tuple[int, int], ...]
    range_starts: tuple[int, ...]
    negated: bool

    @classmethod
    def compile(
        cls,
        literals: set[str],
        ranges: list[tuple[str, str]],
        negated: bool,
        *,
        ignore_case: bool,
    ) -> _CharacterSet:
        normalized_literals = frozenset(
            character.casefold() if ignore_case else character for character in literals
        )
        normalized_ranges: list[tuple[int, int]] = []
        for range_start, range_end in ranges:
            if ignore_case:
                range_start = range_start.casefold()
                range_end = range_end.casefold()
            if len(range_start) != 1 or len(range_end) != 1:
                continue
            start_codepoint = ord(range_start)
            end_codepoint = ord(range_end)
            if start_codepoint <= end_codepoint:
                normalized_ranges.append((start_codepoint, end_codepoint))

        normalized_ranges.sort()
        merged_ranges: list[tuple[int, int]] = []
        for current_start, current_end in normalized_ranges:
            if merged_ranges and current_start <= merged_ranges[-1][1] + 1:
                previous_start, previous_end = merged_ranges[-1]
                merged_ranges[-1] = (
                    previous_start,
                    max(previous_end, current_end),
                )
            else:
                merged_ranges.append((current_start, current_end))

        compiled_ranges = tuple(merged_ranges)
        return cls(
            normalized_literals,
            compiled_ranges,
            tuple(start for start, _ in compiled_ranges),
            negated,
        )

    def matches(self, character: str) -> bool:
        matched = character in self.literals
        if not matched and len(character) == 1 and self.range_starts:
            codepoint = ord(character)
            range_index = bisect_right(self.range_starts, codepoint) - 1
            matched = range_index >= 0 and codepoint <= self.ranges[range_index][1]
        return not matched if self.negated else matched


@dataclass(slots=True)
class _State:
    kind: str
    value: Any = None
    out: int | None = None
    out1: int | None = None


@dataclass(frozen=True, slots=True)
class _Fragment:
    start: int
    outs: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class _LiteralPattern:
    needle: str
    anchored_start: bool
    anchored_end: bool


def _extract_literal_pattern(node: tuple[Any, ...]) -> _LiteralPattern | None:
    nodes = list(node[1:]) if node[0] == "concat" else [node]
    anchored_start = bool(nodes and nodes[0][0] == "start")
    if anchored_start:
        nodes.pop(0)
    anchored_end = bool(nodes and nodes[-1][0] == "end")
    if anchored_end:
        nodes.pop()
    if any(child[0] != "literal" for child in nodes):
        return None
    return _LiteralPattern(
        "".join(child[1] for child in nodes), anchored_start, anchored_end
    )


class _Parser:
    def __init__(self, pattern: str, *, ignore_case: bool = False) -> None:
        self.pattern = pattern
        self.ignore_case = ignore_case
        self.index = 0

    def parse(self) -> tuple[Any, ...]:
        expression = self._alternation()
        if self.index != len(self.pattern):
            if self.pattern[self.index] == ")":
                raise SafeRegexError("unmatched ')' in rg regex")
            raise SafeRegexError(
                f"unsupported rg regex syntax at character {self.index + 1}"
            )
        return expression

    def _alternation(self) -> tuple[Any, ...]:
        branches = [self._sequence()]
        while self._peek() == "|":
            self.index += 1
            branch = self._sequence()
            if branch[0] == "empty":
                raise SafeRegexError("rg regex alternation branches cannot be empty")
            branches.append(branch)
        if len(branches) == 1:
            return branches[0]
        if branches[0][0] == "empty":
            raise SafeRegexError("rg regex alternation branches cannot be empty")
        return ("alt", *branches)

    def _sequence(self) -> tuple[Any, ...]:
        nodes: list[tuple[Any, ...]] = []
        while self.index < len(self.pattern) and self._peek() not in {")", "|"}:
            nodes.append(self._repetition())
        if not nodes:
            return ("empty",)
        if len(nodes) == 1:
            return nodes[0]
        return ("concat", *nodes)

    def _repetition(self) -> tuple[Any, ...]:
        node = self._atom()
        quantifier = self._peek()
        if quantifier in {"*", "+", "?"}:
            self.index += 1
            if self._peek() in {"*", "+", "?"}:
                raise SafeRegexError("repeated rg regex quantifiers are not supported")
            return ({"*": "star", "+": "plus", "?": "optional"}[quantifier], node)
        return node

    def _atom(self) -> tuple[Any, ...]:
        character = self._peek()
        if character is None:
            raise SafeRegexError("unexpected end of rg regex")
        self.index += 1
        if character in {"*", "+", "?"}:
            raise SafeRegexError("rg regex quantifier has no preceding expression")
        if character in {"{", "}"}:
            raise SafeRegexError("rg regex counted repetitions are not supported")
        if character == "]":
            raise SafeRegexError("unmatched ']' in rg regex")
        if character == ".":
            return ("any",)
        if character == "^":
            return ("start",)
        if character == "$":
            return ("end",)
        if character == "[":
            return ("class", self._character_class())
        if character == "(":
            expression = self._alternation()
            if self._peek() != ")":
                raise SafeRegexError("unclosed '(' in rg regex")
            self.index += 1
            if expression[0] == "empty":
                raise SafeRegexError("empty rg regex groups are not supported")
            return expression
        if character == "\\":
            return ("literal", self._escape(in_class=False))
        return ("literal", character)

    def _character_class(self) -> _CharacterSet:
        negated = self._peek() == "^"
        if negated:
            self.index += 1
        literals: set[str] = set()
        ranges: list[tuple[str, str]] = []
        saw_item = False
        while True:
            character = self._peek()
            if character is None:
                raise SafeRegexError("unclosed '[' in rg regex")
            if character == "]":
                self.index += 1
                if not saw_item:
                    raise SafeRegexError(
                        "empty rg regex character classes are unsupported"
                    )
                break
            self.index += 1
            start = self._escape(in_class=True) if character == "\\" else character
            saw_item = True
            if self._peek() == "-" and self._peek(1) not in {None, "]"}:
                self.index += 1
                end_character = self._peek()
                assert end_character is not None
                self.index += 1
                end = (
                    self._escape(in_class=True)
                    if end_character == "\\"
                    else end_character
                )
                if ord(start) > ord(end):
                    raise SafeRegexError("rg regex character class range is reversed")
                ranges.append((start, end))
            else:
                literals.add(start)
        return _CharacterSet.compile(
            literals, ranges, negated, ignore_case=self.ignore_case
        )

    def _escape(self, *, in_class: bool) -> str:
        if self.index >= len(self.pattern):
            raise SafeRegexError("rg regex ends with an incomplete escape")
        character = self.pattern[self.index]
        self.index += 1
        controls = {"n": "\n", "r": "\r", "t": "\t"}
        if character in controls:
            return controls[character]
        allowed = r"\\.^$|?*+()[]{}-" if in_class else r"\\.^$|?*+()[]{}-"
        if character in allowed:
            return character
        raise SafeRegexError(
            f"unsupported rg regex escape \\{character}; "
            "character classes, grouping, alternation, anchors, '.', and '*+?' "
            "are supported, but backreferences and shorthand classes are not"
        )

    def _peek(self, offset: int = 0) -> str | None:
        position = self.index + offset
        return self.pattern[position] if position < len(self.pattern) else None


class SafeRegex:
    """Compiled non-backtracking regex with linear input/state matching cost."""

    def __init__(self, pattern: str, *, ignore_case: bool = False) -> None:
        if not isinstance(pattern, str) or not pattern:
            raise SafeRegexError("rg regex pattern cannot be empty")
        if (
            len(pattern) > MAX_PATTERN_CHARACTERS
            or len(pattern.encode("utf-8")) > MAX_PATTERN_BYTES
        ):
            raise SafeRegexError("rg regex pattern is too long")
        self.pattern = pattern
        self.ignore_case = ignore_case
        self._states: list[_State] = []
        self._ast = _Parser(pattern, ignore_case=ignore_case).parse()
        self._literal_pattern = _extract_literal_pattern(self._ast)
        fragment = self._build(self._ast)
        match = self._add(_State("match"))
        self._patch(fragment.outs, match)
        self._start = fragment.start

    def search(
        self, text: str, *, should_stop: Callable[[], bool] | None = None
    ) -> bool:
        """Return whether the pattern matches anywhere in one logical line."""

        literal_pattern = self._literal_pattern
        if literal_pattern is not None and (
            not self.ignore_case
            or (literal_pattern.needle.isascii() and text.isascii())
        ):
            if should_stop is not None and should_stop():
                return False
            needle = literal_pattern.needle
            haystack = text
            if self.ignore_case:
                needle = needle.lower()
                haystack = haystack.lower()
            if literal_pattern.anchored_start and literal_pattern.anchored_end:
                return haystack == needle
            if literal_pattern.anchored_start:
                return haystack.startswith(needle)
            if literal_pattern.anchored_end:
                return haystack.endswith(needle)
            return needle in haystack

        active = self._closure({self._start}, position=0, length=len(text))
        for position in range(len(text) + 1):
            if should_stop is not None and position % 256 == 0 and should_stop():
                return False
            if any(self._states[index].kind == "match" for index in active):
                return True
            if position == len(text):
                break
            character = text[position]
            candidate = character.casefold() if self.ignore_case else character
            following: set[int] = {self._start}
            for index in active:
                state = self._states[index]
                if state.kind == "literal" and state.value == candidate:
                    assert state.out is not None
                    following.add(state.out)
                elif state.kind == "any":
                    assert state.out is not None
                    following.add(state.out)
                elif state.kind == "class" and state.value.matches(candidate):
                    assert state.out is not None
                    following.add(state.out)
            active = self._closure(following, position=position + 1, length=len(text))
        return False

    def _closure(self, initial: set[int], *, position: int, length: int) -> set[int]:
        active: set[int] = set()
        seen: set[int] = set()
        pending = list(initial)
        while pending:
            index = pending.pop()
            if index in seen:
                continue
            seen.add(index)
            state = self._states[index]
            if state.kind == "split":
                if state.out is not None:
                    pending.append(state.out)
                if state.out1 is not None:
                    pending.append(state.out1)
            elif state.kind == "jump":
                if state.out is not None:
                    pending.append(state.out)
            elif state.kind == "start":
                if position == 0 and state.out is not None:
                    pending.append(state.out)
            elif state.kind == "end":
                if position == length and state.out is not None:
                    pending.append(state.out)
            else:
                active.add(index)
        return active

    def _build(self, node: tuple[Any, ...]) -> _Fragment:
        kind = node[0]
        if kind == "literal":
            value = node[1].casefold() if self.ignore_case else node[1]
            index = self._add(_State(kind, value=value))
            return _Fragment(index, ((index, "out"),))
        if kind == "class":
            index = self._add(_State(kind, value=node[1]))
            return _Fragment(index, ((index, "out"),))
        if kind in {"any", "start", "end", "empty"}:
            state_kind = "jump" if kind == "empty" else kind
            index = self._add(_State(state_kind))
            return _Fragment(index, ((index, "out"),))
        if kind == "concat":
            result = self._build(node[1])
            for child in node[2:]:
                following = self._build(child)
                self._patch(result.outs, following.start)
                result = _Fragment(result.start, following.outs)
            return result
        if kind == "alt":
            fragments = [self._build(child) for child in node[1:]]
            result = fragments[0]
            for following in fragments[1:]:
                split = self._add(
                    _State("split", out=result.start, out1=following.start)
                )
                result = _Fragment(split, result.outs + following.outs)
            return result
        child = self._build(node[1])
        if kind == "star":
            split = self._add(_State("split", out=child.start))
            self._patch(child.outs, split)
            return _Fragment(split, ((split, "out1"),))
        if kind == "plus":
            split = self._add(_State("split", out=child.start))
            self._patch(child.outs, split)
            return _Fragment(child.start, ((split, "out1"),))
        if kind == "optional":
            split = self._add(_State("split", out=child.start))
            return _Fragment(split, child.outs + ((split, "out1"),))
        raise AssertionError(f"unknown regex node: {kind}")

    def _add(self, state: _State) -> int:
        if len(self._states) >= MAX_NFA_STATES:
            raise SafeRegexError("rg regex is too complex")
        self._states.append(state)
        return len(self._states) - 1

    def _patch(self, outs: tuple[tuple[int, str], ...], target: int) -> None:
        for index, attribute in outs:
            setattr(self._states[index], attribute, target)
