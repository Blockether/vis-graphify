"""Add a parameter to a function and an argument to each of its calls.

This module is plain Python over file bytes and reference sites. It knows nothing
about Vis or graphify. A site is any object with `file`, `start`, `end`, `line`
and `role`; `start:end` is the byte range of the name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Characters that open a string or a character literal in each language family.
_QUOTES = {
    ".py": "\"'",
    ".pyi": "\"'",
    ".go": "\"'`",
    ".rs": '"',
    ".clj": '"',
    ".cljs": '"',
    ".cljc": '"',
    ".edn": '"',
}
_DEFAULT_QUOTES = "\"'`"
_CLOSE = {"(": ")", "[": "]", "{": "}"}
_LISP = (".clj", ".cljs", ".cljc", ".edn")
_SKIPPED_ROLES = ("import", "export", "refer", "declaration")


@dataclass(frozen=True)
class Insert:
    """One planned insertion: `text` goes at byte `offset` of `file`."""

    file: str
    line: int
    offset: int
    kind: str
    text: str


@dataclass(frozen=True)
class Plan:
    """The insertions and the reasons that block them."""

    inserts: tuple[Insert, ...]
    conflicts: tuple[str, ...]


def _match(text: str, start: int, quotes: str) -> int | None:
    """Index of the bracket that closes the one at `start`, skipping strings."""
    stack = [_CLOSE[text[start]]]
    i = start + 1
    while i < len(text):
        c = text[i]
        if c in quotes:
            i += 1
            while i < len(text) and text[i] != c:
                i += 2 if text[i] == "\\" else 1
        elif c in _CLOSE:
            stack.append(_CLOSE[c])
        elif c in ")]}":
            if c != stack.pop():
                return None
            if not stack:
                return i
        i += 1
    return None


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _open_after_name(text: str, i: int, quotes: str) -> int | None:
    """Index of the `(` of a call or parameter list after a name, past generics."""
    i = _skip_space(text, i)
    if text.startswith("::<", i):
        i += 2
    if i < len(text) and text[i] == "<":
        depth = 0
        while i < len(text):
            depth += {"<": 1, ">": -1}.get(text[i], 0)
            i += 1
            if depth == 0:
                break
        i = _skip_space(text, i)
    return i if i < len(text) and text[i] == "(" else None


def _append(text: str, open_at: int, close_at: int, item: str, separator: str) -> tuple[int, str]:
    """Offset and text that add `item` as the last element between two brackets."""
    inner = text[open_at + 1 : close_at]
    if not inner.strip():
        return open_at + 1, item
    last = open_at + 1 + len(inner.rstrip())
    if separator == "," and text[last - 1] == ",":
        line_start = text.rfind("\n", 0, last) + 1
        indent = text[line_start : _skip_space(text, line_start)].replace("\n", "")
        return last, f"\n{indent}{item},"
    return last, f"{separator} {item}" if separator else f" {item}"


def _last_item(text: str, open_at: int, close_at: int) -> str:
    """The text after the last top-level comma between two brackets."""
    depth, cut = 0, open_at + 1
    for i in range(open_at + 1, close_at):
        if text[i] in "([{":
            depth += 1
        elif text[i] in ")]}":
            depth -= 1
        elif text[i] == "," and depth == 0:
            cut = i + 1
    return text[cut:close_at].strip().rstrip(",").strip()


def _python_conflict(last: str, item: str, definition: bool) -> str | None:
    keyword = "=" in item.split("(")[0] and "==" not in item
    if last.startswith("**") or (definition and last.startswith("*")):
        return "ends with *args or **kwargs"
    if not keyword and "=" in last.split("(")[0] and "==" not in last:
        return "ends with a default or keyword argument; pass `name=value`"
    return None


def plan(sites, parameter: str, argument: str, sources: dict[str, str]) -> Plan:
    """Plan the insertions for each site. `sources` maps a file to its text.

    A definition gets `parameter` at the end of its parameter list. A call gets
    `argument` at the end of its argument list. Imports and exports stay. A use
    that is not a call, such as a function passed as a value, is a conflict.
    """
    inserts: list[Insert] = []
    conflicts: list[str] = []
    for site in sites:
        if site.role in _SKIPPED_ROLES:
            continue
        text = sources[site.file]
        suffix = Path(site.file).suffix
        quotes = _QUOTES.get(suffix, _DEFAULT_QUOTES)
        start = len(text.encode("utf-8")[: site.start].decode("utf-8", "replace"))
        end = len(text.encode("utf-8")[: site.end].decode("utf-8", "replace"))
        where = f"{site.file}:{site.line}"
        definition = site.role in ("definition", "implementation")
        if suffix in _LISP:
            if definition:
                open_at = _skip_space(text, end)
                if open_at < len(text) and text[open_at] == '"':
                    open_at = _skip_space(text, (_match_string(text, open_at) or open_at) + 1)
                if open_at >= len(text) or text[open_at] != "[":
                    conflicts.append(f"{where}: not one parameter vector; change it by hand.")
                    continue
                close_at = _match(text, open_at, quotes)
                item, separator = parameter, ""
            else:
                head = start
                while head > 0 and text[head - 1] not in ' \t\r\n([{"':
                    head -= 1
                before = text[:head].rstrip()
                if not before.endswith("("):
                    conflicts.append(f"{where}: used as a value, not called; change it by hand.")
                    continue
                open_at = len(before) - 1
                close_at = _match(text, open_at, quotes)
                item, separator = argument, ""
        else:
            open_at = _open_after_name(text, end, quotes)
            if open_at is None:
                conflicts.append(f"{where}: used as a value, not called; change it by hand.")
                continue
            close_at = _match(text, open_at, quotes)
            item, separator = (parameter if definition else argument), ","
        if close_at is None:
            conflicts.append(f"{where}: unbalanced brackets.")
            continue
        if suffix in (".py", ".pyi") and text[open_at + 1 : close_at].strip():
            problem = _python_conflict(_last_item(text, open_at, close_at), item, definition)
            if problem:
                conflicts.append(f"{where}: the {'parameter' if definition else 'argument'} list {problem}.")
                continue
        offset, insert = _append(text, open_at, close_at, item, separator)
        byte = len(text[:offset].encode("utf-8"))
        inserts.append(Insert(site.file, site.line, byte, "definition" if definition else "call", insert))
    if not any(i.kind == "definition" for i in inserts) and not conflicts:
        conflicts.append("No definition with a parameter list was found.")
    return Plan(tuple(inserts), tuple(conflicts))


def _match_string(text: str, start: int) -> int | None:
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return None


def rewrite(plan_: Plan, sources: dict[str, bytes]) -> dict[str, bytes]:
    """The new bytes of each changed file."""
    out: dict[str, bytes] = {}
    for file in {i.file for i in plan_.inserts}:
        data = sources[file]
        for insert in sorted((i for i in plan_.inserts if i.file == file), key=lambda i: i.offset, reverse=True):
            data = data[: insert.offset] + insert.text.encode("utf-8") + data[insert.offset :]
        out[file] = data
    return out
