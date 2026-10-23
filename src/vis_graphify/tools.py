"""The graphify tools that Vis exposes as `graphify.*`.

This module is plain Python. `extension.py` binds it to Vis.
"""

from __future__ import annotations

import base64
import dataclasses
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable

from vis_graphify import graph as g
from vis_graphify import kb


@dataclass(frozen=True)
class Node:
    """One graph node."""

    id: Annotated[str, "Stable node id; pass it to other calls for an exact match."]
    label: Annotated[str, "Display name, such as `my.ns` or `run-tests()`."]
    file: Annotated[str, "Source file, relative to the project root."]
    line: Annotated[int | None, "1-based line of the definition, if known."]
    kind: Annotated[str, "Language kind, such as `namespace` or `var`, or the file type."]
    community: Annotated[int | None, "Cluster number, if the graph was clustered."]

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{self.label}  {where}  [{self.id}]" if where else f"{self.label}  [{self.id}]"


@dataclass(frozen=True)
class Edge:
    """One edge seen from a node."""

    relation: Annotated[str, "Edge type: calls, references, imports, contains, ..."]
    node: Annotated[Node, "The node at the other end."]
    confidence: Annotated[str, "EXTRACTED for parsed facts, INFERRED for guesses."]
    at: Annotated[str, "Where the edge occurs, as `file:Lline`."]

    def __str__(self) -> str:
        return f"{self.relation} {self.node}"


@dataclass(frozen=True)
class BuildResult:
    """The outcome of one graph build. A failed build raises an error with the graphify output."""

    root: Annotated[str, "Absolute project root."]
    graph_path: Annotated[str, "Absolute path of graph.json."]
    nodes: Annotated[int, "Node count after the build."]
    edges: Annotated[int, "Edge count after the build."]
    seconds: Annotated[float, "Wall time of the build."]

    def __str__(self) -> str:
        return f"Graph built: {self.nodes} nodes, {self.edges} edges in {self.seconds:g} s."


@dataclass(frozen=True)
class GraphStatus:
    """Facts about the graph of a project."""

    root: Annotated[str, "Absolute project root."]
    graph_path: Annotated[str, "Absolute path of graph.json."]
    exists: Annotated[bool, "False until graphify.build() runs."]
    nodes: Annotated[int, "Node count."]
    edges: Annotated[int, "Edge count."]
    files: Annotated[int, "Count of source files in the graph."]
    relations: Annotated[dict[str, int], "Edge count for each relation."]
    built_at: Annotated[str, "Modification time of graph.json, ISO 8601, or empty."]

    def __str__(self) -> str:
        if not self.exists:
            return "No graph yet. Any graphify query builds it."
        return f"Graph: {self.nodes} nodes, {self.edges} edges, {self.files} files, built {self.built_at}."


@dataclass(frozen=True)
class SearchResult:
    """Nodes that match a pattern."""

    pattern: Annotated[str, "The case-insensitive regex."]
    total: Annotated[int, "Count of all matches."]
    nodes: Annotated[tuple[Node, ...], "The best matches, labels first, then by degree."]

    def __str__(self) -> str:
        return _listing(f"{len(self.nodes)} of {self.total} nodes match {self.pattern!r}:", self.nodes, "nodes")


@dataclass(frozen=True)
class Explanation:
    """One node with its direct neighbours."""

    node: Annotated[Node, "The node that was asked about."]
    outgoing: Annotated[tuple[Edge, ...], "Edges from the node: what it calls, references or contains."]
    incoming: Annotated[tuple[Edge, ...], "Edges to the node: callers, importers, its container."]
    truncated: Annotated[bool, "True when `limit` cut either list."]

    def __str__(self) -> str:
        return "\n".join(
            [
                str(self.node),
                _listing("Uses:", self.outgoing, "outgoing"),
                _listing("Used by:", self.incoming, "incoming"),
            ]
        )


@dataclass(frozen=True)
class PathResult:
    """The shortest connection between two nodes."""

    source: Annotated[Node, "Start node."]
    target: Annotated[Node, "End node."]
    found: Annotated[bool, "False when the nodes are not connected."]
    steps: Annotated[tuple[Edge, ...], "Each hop: the relation and the node it reaches."]

    def __str__(self) -> str:
        if not self.found:
            return f"No path from {self.source.label} to {self.target.label}."
        hops = " ".join(f"-{step.relation}-> {step.node.label}" for step in self.steps)
        return f"{self.source.label} {hops}"


@dataclass(frozen=True)
class Hub:
    """One highly connected node."""

    node: Annotated[Node, "The node."]
    degree: Annotated[int, "Count of counted edges."]

    def __str__(self) -> str:
        return f"{self.degree:>4}  {self.node}"


@dataclass(frozen=True)
class HubsResult:
    """The most connected nodes."""

    hubs: Annotated[tuple[Hub, ...], "Nodes with their degree, highest first."]

    def __str__(self) -> str:
        return _listing("Degree, node:", self.hubs, "hubs")


@dataclass(frozen=True)
class Impact:
    """One node that depends on a changed node."""

    node: Annotated[Node, "The dependent node."]
    distance: Annotated[int, "Steps from the changed node."]
    via: Annotated[str, "Relation through which it depends, such as `calls`."]
    chain: Annotated[tuple[str, ...], "Labels of the nodes between it and the changed node, nearest to it first."] = ()

    def __str__(self) -> str:
        line = f"{self.distance}  {self.via:<10} {self.node}"
        return f"{line}  via {' -> '.join(self.chain)}" if self.chain else line


@dataclass(frozen=True)
class ImpactResult:
    """What a change to a node can reach."""

    node: Annotated[Node, "The changed node."]
    depth: Annotated[int, "The depth limit, or the largest distance reached when there was no limit."]
    files: Annotated[tuple[str, ...], "Distinct source files of the dependents."]
    impacts: Annotated[tuple[Impact, ...], "Dependents, nearest first."]
    truncated: Annotated[bool, "True when `limit` cut the list."]
    others: Annotated[
        tuple[str, ...],
        "Other nodes with the same name, which are not this node, each with the files of its direct users.",
    ] = ()
    functions: Annotated[tuple[str, ...], "Sorted names of the dependent functions and methods."] = ()
    private: Annotated[tuple[str, ...], "The names in `functions` that are private or not exported."] = ()

    def __str__(self) -> str:
        more = " (cut by `limit`)" if self.truncated else ""
        rows = [impact for impact in self.impacts if not _is_file(impact.node)]
        header = f"{len(rows)} dependents of {self.node.label} in {len(self.files)} files{more}. Distance, relation, node, then the nodes between:"
        lines = [_listing(header, rows, "impacts")]
        name = self.node.label.removesuffix("()")
        if self.others:
            lines.append(f"Different nodes also named {name}; their users are not in this list:")
            lines += [f"  {other}" for other in self.others]
        if self.functions:
            lines.append(f"Dependent functions{'' if self.truncated else ', complete'}: {', '.join(self.functions)}")
            if self.private:
                lines.append(f"Private among them: {', '.join(self.private)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class DomainFinding:
    """One problem or note that a domain check reported."""

    severity: Annotated[str, "error, warning or info."]
    code: Annotated[str, "Check name, such as `missing-command` or `event-not-applied`."]
    message: Annotated[str, "What is wrong, in one sentence."]
    file: Annotated[str, "Source file, relative to the project root, or empty."]
    line: Annotated[int | None, "1-based line, if known."]
    element: Annotated[str, "The element as `kind:service:name`, or empty."]
    plugin: Annotated[str, "The plugin that reported it."]

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{self.severity} {self.code} {where} {self.message}".replace("  ", " ")


@dataclass(frozen=True)
class DomainReport:
    """The domain model of a project: elements, links, schemas and findings."""

    root: Annotated[str, "Absolute project root."]
    markdown_path: Annotated[str, "DOMAIN.md with diagrams, flows and schemas, or empty if no plugin applies."]
    json_path: Annotated[str, "domain.json with the full model, or empty."]
    plugins: Annotated[tuple[str, ...], "Plugins that ran. Empty when no plugin applies."]
    services: Annotated[tuple[str, ...], "Services and client namespaces."]
    counts: Annotated[dict[str, int], "Element count for each kind, such as `command` or `event`."]
    relations: Annotated[int, "Count of links: emits, reads, invokes, triggers, ..."]
    schemas: Annotated[int, "Count of JSON Schemas converted from Malli."]
    errors: Annotated[int, "Count of error findings, such as calls to missing commands."]
    warnings: Annotated[int, "Count of warning findings."]
    notes: Annotated[int, "Count of info findings."]
    findings: Annotated[tuple[DomainFinding, ...], "Findings, errors first."]
    plugin_errors: Annotated[tuple[str, ...], "Plugins that failed to load or run."]
    truncated: Annotated[bool, "True when `limit` cut the findings."]

    def __str__(self) -> str:
        if not self.plugins:
            return "No domain plugin applies to this project."
        counts = ", ".join(f"{count} {kind}" for kind, count in self.counts.items())
        header = (
            f"Domain ({', '.join(self.plugins)}): {counts}, {self.relations} links. "
            f"{self.errors} errors, {self.warnings} warnings. Diagrams: {self.markdown_path}"
        )
        return _listing(header, self.findings, "findings", 20)


@dataclass(frozen=True)
class Reference:
    """One exact site of a symbol."""

    file: Annotated[str, "Source file, relative to the project root."]
    line: Annotated[int, "1-based line."]
    column: Annotated[int, "1-based column of the name."]
    role: Annotated[
        str, "definition, use, member, import or export; Clojure adds refer, declaration and implementation."
    ]
    text: Annotated[str, "The name as written; an import alias differs from the symbol name."]
    qualified: Annotated[bool, "True for a qualified site: `obj.name`, `mod::name` or `ns/name`."]

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.column}  {self.role}  {self.text}"


@dataclass(frozen=True)
class ReferencesResult:
    """Every site that resolves to one symbol."""

    node: Annotated[Node, "The symbol."]
    total: Annotated[int, "Count of all sites."]
    files: Annotated[tuple[str, ...], "Distinct files with a site."]
    references: Annotated[tuple[Reference, ...], "Sites in file and position order."]
    truncated: Annotated[bool, "True when `limit` cut the list."]

    def __str__(self) -> str:
        header = f"{self.total} sites of {self.node.label} in {len(self.files)} files:"
        return _listing(header, self.references, "references")


@dataclass(frozen=True)
class RenameEdit:
    """One replacement of a name."""

    file: Annotated[str, "Source file, relative to the project root."]
    line: Annotated[int, "1-based line."]
    column: Annotated[int, "1-based column of the name."]
    old: Annotated[str, "Text before, such as `helper` or `->Rec`."]
    new: Annotated[str, "Text after."]


@dataclass(frozen=True)
class RenameResult:
    """A rename preview, or the rename that was written."""

    node: Annotated[Node, "The symbol."]
    old: Annotated[str, "Old name."]
    new: Annotated[str, "New name."]
    applied: Annotated[bool, "True when the files were written."]
    files: Annotated[tuple[str, ...], "Files that the rename changes."]
    edits: Annotated[tuple[RenameEdit, ...], "Every replacement."]
    conflicts: Annotated[tuple[str, ...], "Reasons that make the rename unsafe. With a conflict, nothing is written."]
    mentions: Annotated[
        tuple[str, ...], "`file:line text` of other code symbols that spell the old name and stay. Check them."
    ]
    text_mentions: Annotated[int, "Count of strings, comments and keywords that spell the old name and stay."]
    diff: Annotated[str, "Unified diff of the change."]
    changes: Annotated[tuple[str, ...], "`file:line text` of each changed line, as it reads after the change."] = ()
    check: Annotated[
        str,
        "The project check that ran after the write, such as `go vet ./...: passed`, with the first error lines on failure. Empty when none ran.",
    ] = ""

    def __str__(self) -> str:
        """A short summary: counts, conflicts, each changed line and code mentions."""
        change = f"{self.old} -> {self.new}: {len(self.edits)} edits in {len(self.files)} files"
        if self.conflicts:
            return "\n".join([f"Blocked {change}. Nothing was written. Conflicts:", *self.conflicts])
        lines = [f"Renamed {change}:" if self.applied else f"Preview {change}. Call again with apply=True to write:"]
        lines.append(_change_lines(self.changes) if self.changes else _clip_lines(self.diff, 60))
        if self.mentions:
            more = [f"… {len(self.mentions) - 10} more in `mentions`."] if len(self.mentions) > 10 else []
            lines += [f"Other code that spells {self.old} and stays:", *self.mentions[:10], *more]
        else:
            lines.append(f"No other code spells {self.old}.")
        if self.text_mentions:
            lines.append(f"Kept {self.text_mentions} mentions in strings, comments and keywords.")
        if self.check:
            lines.append(f"Check {self.check}")
        return "\n".join(lines)


@dataclass(frozen=True)
class CallEdit:
    """One added parameter or argument."""

    file: Annotated[str, "Source file, relative to the project root."]
    line: Annotated[int, "1-based line of the name."]
    kind: Annotated[str, "`definition` or `call`."]
    text: Annotated[str, "The inserted text, such as `, currency string`."]


@dataclass(frozen=True)
class SignatureResult:
    """A parameter change preview, or the change that was written."""

    node: Annotated[Node, "The function or method."]
    parameter: Annotated[str, "The added parameter, as written in the definition."]
    argument: Annotated[str, "The argument added to each call."]
    applied: Annotated[bool, "True when the files were written."]
    files: Annotated[tuple[str, ...], "Files that the change touches."]
    edits: Annotated[tuple[CallEdit, ...], "Every insertion."]
    conflicts: Annotated[tuple[str, ...], "Sites that block the change. With a conflict, nothing is written."]
    diff: Annotated[str, "Unified diff of the change."]
    changes: Annotated[tuple[str, ...], "`file:line text` of each changed line, as it reads after the change."] = ()
    check: Annotated[
        str,
        "The project check that ran after the write, such as `go vet ./...: passed`, with the first error lines on failure. Empty when none ran.",
    ] = ""

    def __str__(self) -> str:
        """A short summary: counts, conflicts and each changed line."""
        calls = sum(1 for edit in self.edits if edit.kind == "call")
        change = f"parameter {self.parameter} of {self.node.label}: {calls} calls in {len(self.files)} files"
        if self.conflicts:
            return "\n".join([f"Blocked {change}. Nothing was written. Conflicts:", *self.conflicts])
        head = f"Added {change}:" if self.applied else f"Preview {change}. Call again with apply=True to write:"
        lines = [head, _change_lines(self.changes) if self.changes else _clip_lines(self.diff, 60)]
        if self.check:
            lines.append(f"Check {self.check}")
        return "\n".join(lines)


@dataclass(frozen=True)
class MoveResult:
    """A move preview, or the move that was written."""

    subject: Annotated[str, "What moves: a definition name or a file."]
    source: Annotated[str, "The old file, relative to the project root."]
    target: Annotated[str, "The new file, relative to the project root."]
    applied: Annotated[bool, "True when the files were written."]
    files: Annotated[tuple[str, ...], "Every file that the move writes, creates or deletes."]
    created: Annotated[tuple[str, ...], "New files."]
    deleted: Annotated[tuple[str, ...], "Removed files: the old file of a file move."]
    renames: Annotated[tuple[str, ...], "`old -> new` module or namespace names, such as test namespaces."]
    conflicts: Annotated[tuple[str, ...], "Reasons that make the move unsafe. With a conflict, nothing is written."]
    notes: Annotated[tuple[str, ...], "Side changes, such as a visibility change or an import that moved."]
    mentions: Annotated[tuple[str, ...], "`file:line text` of code that names the old place and stays. Check them."]
    text_mentions: Annotated[int, "Count of strings, comments and data that name the old place and stay."]
    diff: Annotated[str, "Unified diff of the change."]
    check: Annotated[
        str,
        "The project check that ran after the write, such as `go vet ./...: passed`, with the first error lines on failure. Empty when none ran.",
    ] = ""

    def __str__(self) -> str:
        """A short summary: the move, conflicts, renames, notes, code mentions and the diff."""
        change = f"{self.subject}: {self.source} -> {self.target}, {len(self.files)} files"
        if self.conflicts:
            return "\n".join([f"Blocked move of {change}. Nothing was written. Conflicts:", *self.conflicts])
        lines = [
            f"Moved {change}:" if self.applied else f"Preview move of {change}. Call again with apply=True to write:"
        ]
        if self.renames:
            lines.append("Renamed: " + ", ".join(self.renames))
        lines += self.notes
        if self.mentions:
            more = [f"… {len(self.mentions) - 10} more in `mentions`."] if len(self.mentions) > 10 else []
            lines += ["Other code that names the old place and stays:", *self.mentions[:10], *more]
        if self.text_mentions:
            lines.append(f"Kept {self.text_mentions} mentions in strings, comments and data.")
        lines.append(_clip_lines(self.diff, 80))
        if self.check:
            lines.append(f"Check {self.check}")
        return "\n".join(lines)


def _relative(root: Path, file: str) -> str:
    path = Path(file)
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else file


def _within(file: str | None, scope: str) -> bool:
    """True when `file` is in the folder or file `scope`, a path prefix or a glob."""
    if not file:
        return False
    scope = scope.removeprefix("./")
    if any(c in scope for c in "*?["):
        return fnmatch.fnmatch(file, scope)
    return file == scope or file.startswith(scope.rstrip("/") + "/")


def _raw(graph: g.Graph) -> dict:
    """The graph.json shape that `graphify.refactor` reads, from the cached graph."""
    return {"nodes": list(graph.nodes.values()), "links": [dataclasses.asdict(link) for link in graph.links]}


def _listing(header: str, rows, field: str, limit: int = 40) -> str:
    """`header`, then one line for each of the first `limit` rows, then a count of the rest."""
    lines = [header] + [f"  {row}" for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"  … {len(rows) - limit} more in `{field}`.")
    return "\n".join(lines) if rows else f"{header} none"


def _change_lines(changes: tuple[str, ...], limit: int = 80) -> str:
    """The first `limit` changed lines, with a count of the rest."""
    more = [f"… {len(changes) - limit} more in `changes`."] if len(changes) > limit else []
    return "\n".join([*changes[:limit], *more])


def _changed(root: Path, new: dict[str, bytes], sites) -> tuple[str, ...]:
    """`file:line text` of each `(file, line)` site, read from the new bytes of its file."""
    rows = {}
    for file, line in sites:
        lines = new[file].decode("utf-8", "replace").splitlines() if file in new else []
        if 0 < line <= len(lines):
            rows[(_relative(root, file), line)] = lines[line - 1].strip()
    return tuple(f"{file}:{line}  {text}" for (file, line), text in sorted(rows.items()))


def _clip_lines(text: str, limit: int) -> str:
    """The first `limit` lines of `text`, with a count of the lines that were cut."""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.rstrip("\n")
    return "\n".join(lines[:limit] + [f"… {len(lines) - limit} more lines in `diff`."])


_CHECK_TIMEOUT_S = 300


def _cargo_target(root: Path) -> str:
    """A target folder outside the project, so that `cargo check` adds no files to it."""
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    return str(Path(tempfile.gettempdir()) / "vis-graphify-cargo" / digest)


def _check_command(root: Path) -> tuple[list[str], dict | None] | None:
    """The fast compile or lint check of the project at `root`, or None."""
    if (root / "go.mod").exists() and shutil.which("go"):
        return ["go", "vet", "./..."], None
    if (root / "tsconfig.json").exists():
        local = root / "node_modules" / ".bin" / "tsc"
        tsc = str(local) if local.exists() else shutil.which("tsc")
        if tsc:
            return [tsc, "--noEmit", "-p", "."], None
    if (root / "Cargo.toml").exists() and shutil.which("cargo"):
        return ["cargo", "check", "-q"], {
            **os.environ,
            "CARGO_TARGET_DIR": os.environ.get("CARGO_TARGET_DIR") or _cargo_target(root),
        }
    if ((root / "deps.edn").exists() or (root / "project.clj").exists()) and shutil.which("clj-kondo"):
        return ["clj-kondo", "--lint", *([d for d in ("src", "test") if (root / d).is_dir()] or ["."])], None
    return None


def _python_check(root: Path, files) -> str:
    """Compile each changed Python file in memory; nothing is written."""
    sources = [root / file for file in files if str(file).endswith(".py")]
    errors = []
    for source in sources:
        try:
            compile(source.read_text(encoding="utf-8"), str(source), "exec")
        except SyntaxError as error:
            errors.append(f"{_relative(root, str(source))}:{error.lineno} {error.msg}")
    if not sources:
        return ""
    label = f"python syntax of {len(sources)} files"
    return "\n".join([f"{label}: failed", *errors]) if errors else f"{label}: passed"


def project_check(root: Path, files, run: Runner = subprocess.run) -> str:
    """Run the fast check of the project after a write: `command: passed`, or `command: failed` and the first errors."""
    found = _check_command(root)
    if found is None:
        return _python_check(root, files)
    argv, env = found
    label = " ".join([Path(argv[0]).name, *argv[1:]])
    try:
        done = run(argv, cwd=str(root), env=env, capture_output=True, text=True, timeout=_CHECK_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"{label}: not run ({type(error).__name__})"
    kondo = argv[0] == "clj-kondo"
    if done.returncode < (3 if kondo else 1):
        return f"{label}: passed"
    lines = [line for line in ((done.stdout or "") + (done.stderr or "")).splitlines() if line.strip()]
    if kondo:
        lines = [line for line in lines if ": error:" in line] or lines
    return "\n".join([f"{label}: failed", *lines[:20]])


def _unified_diff(root: Path, contents: dict) -> str:
    chunks = []
    for file, (old, new) in sorted(contents.items()):
        name = _relative(root, file)
        # A created file has no old bytes and a deleted file has no new bytes.
        chunks.extend(
            difflib.unified_diff(
                (old or b"").decode("utf-8", "replace").splitlines(keepends=True),
                (new or b"").decode("utf-8", "replace").splitlines(keepends=True),
                f"a/{name}" if old is not None else "/dev/null",
                f"b/{name}" if new is not None else "/dev/null",
            )
        )
    return "".join(chunks)


def _line(location: str) -> int | None:
    digits = location.lstrip("Ll").split(":")[0].split("-")[0]
    return int(digits) if digits.isdigit() else None


def _node(graph: g.Graph, node_id: str) -> Node:
    raw = graph.nodes.get(node_id, {"id": node_id, "label": node_id})
    metadata = raw.get("metadata") or {}
    community = raw.get("community")
    return Node(
        id=node_id,
        label=str(raw.get("label", node_id)),
        file=str(raw.get("source_file") or ""),
        line=_line(str(raw.get("source_location") or "")),
        kind=str(metadata.get("kind") or raw.get("file_type") or ""),
        community=community if isinstance(community, int) else None,
    )


def _edge(graph: g.Graph, link: g.Link, other: str) -> Edge:
    return Edge(
        relation=link.relation,
        node=_node(graph, other),
        confidence=link.confidence,
        at=f"{link.source_file}:{link.source_location}" if link.source_file else "",
    )


def graphify_command() -> list[str]:
    """The argv prefix that runs the graphify CLI of this extension.

    Vis installs graphify in the private environment of this package. That
    environment has its own interpreter, which a child process can run.
    """
    import graphify

    package = Path(graphify.__file__).absolute().parent
    for parent in package.parents:
        if (parent / "pyvenv.cfg").is_file():
            for python in (parent / "bin" / "python", parent / "Scripts" / "python.exe"):
                if python.exists():
                    return [str(python), "-m", "graphify"]
            break
    found = shutil.which("graphify")
    if found:
        return [found]
    return [sys.executable, "-m", "graphify"]


def _child_env() -> dict[str, str]:
    """The environment for graphify: no host Python paths that could shadow its packages."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _ignore_output(out: Path) -> None:
    """Keep graphify output out of git, grep and ls: add `.gitignore` with `*` unless one exists."""
    ignore = out / ".gitignore"
    if out.is_dir() and not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


BUNDLED_PLUGINS = Path(__file__).resolve().parent / "domain_plugins"
"""Domain plugins that ship with this extension. graphify loads every `*.py` file here."""


def _plugin_env(plugins: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    """The child environment with the bundled plugins and `plugins` in `GRAPHIFY_PLUGINS`."""
    env = _child_env()
    specs = [str(BUNDLED_PLUGINS), *plugins]
    if env.get("GRAPHIFY_PLUGINS"):
        specs.append(env["GRAPHIFY_PLUGINS"])
    env["GRAPHIFY_PLUGINS"] = os.pathsep.join(specs)
    return env


def _is_file(node: Node) -> bool:
    """True for the node of a whole source file, such as `h04.py`."""
    return bool(node.file) and node.label == Path(node.file).name


_PRIVATE_DEFINITION = re.compile(r"^\s*(\(defn-|\(def\s+\^:private|private\s|fn\s|func\s+[a-z_])")


def _private(root: Path, node: Node) -> bool:
    """True when the definition of `node` is private or not exported, from its name and its definition line."""
    name = node.label.removesuffix("()")
    if name.startswith("_"):
        return True
    try:
        text = (root / node.file).read_text(encoding="utf-8", errors="replace").splitlines()[node.line - 1]
    except (OSError, IndexError, TypeError):
        return False
    if node.file.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
        return text.startswith(("function ", "async function ", "const ", "let ", "class "))
    if node.file.endswith(".go"):
        return name[:1].islower()
    return bool(_PRIVATE_DEFINITION.match(text))


def _others(graph: g.Graph, node_id: str, limit: int = 5) -> tuple[str, ...]:
    """Other code nodes with the same label as `node_id`, each with the files of its direct users."""
    label = graph.nodes[node_id].get("label")
    rows = []
    for other_id, data in graph.nodes.items():
        if other_id == node_id or data.get("label") != label or data.get("file_type") != "code":
            continue
        other = _node(graph, other_id)
        users = sorted(
            {
                _node(graph, link.source).file
                for link in graph.incoming.get(other_id, ())
                if link.relation in g.DEPENDENT_RELATIONS and _node(graph, link.source).file
            }
        )
        used = f"used in {', '.join(users)}" if users else "not used"
        rows.append(f"{other.label} {other.file}:{other.line or 1} [{other_id}], {used}")
    return tuple(rows[:limit])


Runner = Callable[..., subprocess.CompletedProcess]


class GraphifyTools:
    """Build and query a graphify knowledge graph of the workspace code."""

    def __init__(
        self,
        workspace_root: Callable[[], str] | None = None,
        runner: Runner | None = None,
        checker: Callable[[Path, list[str]], str] | None = None,
        spawn: Callable[..., subprocess.Popen] | None = None,
        kb_actor: str = "agent",
    ):
        self._workspace_root = workspace_root or os.getcwd
        self._run = runner or subprocess.run
        self._check = checker or project_check
        self._spawn = spawn or subprocess.Popen
        self._kb_actor = kb_actor
        self._kb_url: str | None = None
        self._kb_servers: dict[str, subprocess.Popen] = {}

    def _root(self, root: str) -> Path:
        path = Path(root).expanduser()
        if not path.is_absolute():
            path = Path(self._workspace_root()) / path
        path = path.resolve()
        if not path.is_dir():
            raise NotADirectoryError(f"Project root {path} is not a directory.")
        return path

    def build(
        self,
        root: str = ".",
        force: bool = False,
        cluster: bool = True,
        timeout_s: int = 1800,
        plugins: list[str] | None = None,
    ) -> BuildResult:
        """Build or update the code graph of a project with graphify (AST only, no LLM).

        Writes `<root>/graphify-out/graph.json` and `GRAPH_REPORT.md`, with a `.gitignore`
        that keeps them out of git and grep. A later build re-extracts only changed
        files. Supports Clojure, Python,
        JavaScript, TypeScript, Java, Go, Rust and the other graphify languages.
        When a domain plugin applies, the graph also gets its commands, queries
        and events as `domain:<kind>:<service>:<name>` nodes.

        Args:
            root: Project directory, relative to the workspace root.
            force: Overwrite graph.json even if the new graph has fewer nodes.
            cluster: Detect communities. False is faster on large projects.
            timeout_s: Stop graphify after this many seconds.
            plugins: Extra domain plugin specs: a name, `module:attr` or a `.py` file or directory.

        Returns:
            BuildResult with the counts. A failed build raises an error with the graphify output.
        """
        return self._update(self._root(root), force, cluster, timeout_s, plugins)

    def _update(
        self,
        path: Path,
        force: bool = False,
        cluster: bool = True,
        timeout_s: int = 1800,
        plugins: list[str] | None = None,
    ) -> BuildResult:
        """Run `graphify update` on a resolved project root."""
        argv = graphify_command() + ["update", str(path)]
        if force:
            argv.append("--force")
        if not cluster:
            argv.append("--no-cluster")
        started = time.monotonic()
        try:
            done = self._run(
                argv, cwd=str(path), env=_plugin_env(plugins or ()), capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"graphify did not finish in {timeout_s} s for {path}.") from error
        seconds = round(time.monotonic() - started, 2)
        output = _tail((done.stdout or "") + "\n" + (done.stderr or ""))
        nodes = edges = 0
        if g.graph_path(path).is_file():
            built = g.load(path)
            nodes, edges = len(built.nodes), len(built.links)
        if done.returncode != 0:
            raise RuntimeError(f"graphify exited with status {done.returncode}.\n{output}")
        _ignore_output(g.graph_path(path).parent)
        return BuildResult(
            root=str(path),
            graph_path=str(g.graph_path(path)),
            nodes=nodes,
            edges=edges,
            seconds=seconds,
        )

    def _graph(self, path: Path) -> g.Graph:
        """The graph of a project. Builds it when it is missing, updates it when a source file changed."""
        if not g.graph_path(path).is_file():
            self._update(path)
        elif g.stale(g.load(path), path):
            self._update(path)
        return g.load(path)

    def status(self, root: str = ".") -> GraphStatus:
        """Report whether a project has a graph, and its size and age. Builds nothing.

        Args:
            root: Project directory, relative to the workspace root.
        """
        path = self._root(root)
        file = g.graph_path(path)
        if not file.is_file():
            return GraphStatus(str(path), str(file), False, 0, 0, 0, {}, "")
        graph = g.load(path)
        relations: dict[str, int] = {}
        for link in graph.links:
            relations[link.relation] = relations.get(link.relation, 0) + 1
        files = {str(n.get("source_file")) for n in graph.nodes.values() if n.get("source_file")}
        built = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(file.stat().st_mtime))
        return GraphStatus(
            str(path),
            str(file),
            True,
            len(graph.nodes),
            len(graph.links),
            len(files),
            dict(sorted(relations.items(), key=lambda item: -item[1])),
            built,
        )

    def search(self, pattern: str, root: str = ".", limit: int = 25) -> SearchResult:
        """Find graph nodes whose label, id or file matches a case-insensitive regex.

        Args:
            pattern: Regex, for example `run-tests` or `gateway/.*client`. An invalid regex, such as `*out*`, matches as text.
            root: Project directory, relative to the workspace root.
            limit: Maximum count of returned nodes.
        """
        graph = self._graph(self._root(root))
        ids, total = g.search(graph, pattern, limit)
        return SearchResult(pattern, total, tuple(_node(graph, node_id) for node_id in ids))

    def explain(self, node: str, root: str = ".", limit: int = 40) -> Explanation:
        """Show one node with what it calls and what calls or contains it.

        Args:
            node: Node id, label (`run-tests()`), bare name or a qualified name (`ns/name`, `module.name`, `Class.method`, `crate::m::name`) or `file::name`.
            root: Project directory, relative to the workspace root.
            limit: Maximum count of edges in each direction.
        """
        graph = self._graph(self._root(root))
        node_id = g.resolve(graph, node)
        out = graph.outgoing.get(node_id, [])
        inc = graph.incoming.get(node_id, [])
        return Explanation(
            node=_node(graph, node_id),
            outgoing=tuple(_edge(graph, link, link.target) for link in out[:limit]),
            incoming=tuple(_edge(graph, link, link.source) for link in inc[:limit]),
            truncated=len(out) > limit or len(inc) > limit,
        )

    def shortest_path(self, source: str, target: str, root: str = ".") -> PathResult:
        """Find the shortest chain of edges that connects two nodes, in either direction.

        Args:
            source: Start node: id, label or name.
            target: End node: id, label or name.
            root: Project directory, relative to the workspace root.
        """
        graph = self._graph(self._root(root))
        start, end = g.resolve(graph, source), g.resolve(graph, target)
        steps = g.shortest_path(graph, start, end)
        return PathResult(
            source=_node(graph, start),
            target=_node(graph, end),
            found=bool(steps),
            steps=tuple(_edge(graph, link, node_id) for node_id, link in steps if link is not None),
        )

    def hubs(self, root: str = ".", limit: int = 20, relation: str | None = "calls") -> HubsResult:
        """List the most connected nodes: the code that much else depends on.

        Args:
            root: Project directory, relative to the workspace root.
            limit: Count of nodes to return.
            relation: Count only this relation, such as `calls` or `imports`. None counts all.
        """
        graph = self._graph(self._root(root))
        relations = frozenset({relation}) if relation else None
        return HubsResult(
            tuple(Hub(_node(graph, node_id), degree) for node_id, degree in g.hubs(graph, limit, relations))
        )

    def affected(
        self, node: str, root: str = ".", depth: int | None = None, limit: int = 100, within: str | None = None
    ) -> ImpactResult:
        """List the code that a change to a node can break: its callers, importers and users.

        Args:
            node: Changed node: id, label, name or a qualified name (`ns/name`, `module.name`, `Class.method`, `crate::m::name`) or `file::name`.
            root: Project directory, relative to the workspace root.
            depth: How many dependency steps to follow back. None follows every step.
            limit: Maximum count of returned dependents.
            within: Keep only dependents in this folder or file: a path prefix such as
                `src/handlers/` or a glob such as `**/*_test.py`. The search still follows every step.
        """
        path = self._root(root)
        graph = self._graph(path)
        node_id = g.resolve(graph, node)
        rows = g.affected(graph, node_id, max(1, depth) if depth else len(graph.nodes))
        target = {other: link.target for other, _, link in rows}
        if within:
            rows = [row for row in rows if _within(_node(graph, row[0]).file, within)]

        def chain(other: str) -> tuple[str, ...]:
            labels, current = [], target[other]
            while current != node_id and current in target and len(labels) < len(target):
                labels.append(_node(graph, current).label)
                current = target[current]
            return tuple(labels)

        impacts = tuple(
            Impact(_node(graph, other), distance, link.relation, chain(other)) for other, distance, link in rows[:limit]
        )
        files = tuple(sorted({impact.node.file for impact in impacts if impact.node.file}))
        reached = max((impact.distance for impact in impacts), default=0)
        callers = {
            impact.node.label.removesuffix("()"): impact.node for impact in impacts if impact.node.label.endswith("()")
        }
        return ImpactResult(
            _node(graph, node_id),
            depth or reached,
            files,
            impacts,
            len(rows) > limit,
            _others(graph, node_id),
            tuple(sorted(callers)),
            tuple(sorted(name for name, caller in callers.items() if _private(path, caller))),
        )

    def references(self, node: str, root: str = ".", limit: int = 200) -> ReferencesResult:
        """List every exact site of a symbol: its definition, uses, member calls and imports.

        Works for functions, methods, classes and Clojure vars in every language with a
        tree-sitter grammar. Resolution follows imports, aliases, receiver types and
        locals, so a parameter, a field or another symbol with the same name never counts.

        Args:
            node: The symbol: id, label or a qualified name (`ns/name`, `module.name`, `Class.method`, `crate::m::name`) or `file::name`.
            root: Project directory, relative to the workspace root.
            limit: Maximum count of returned sites.
        """
        from graphify import refactor

        path = self._root(root)
        graph = self._graph(path)
        node_id = g.resolve(graph, node)
        sites = refactor.references(_raw(graph), node_id, path)
        found = tuple(Reference(_relative(path, s.file), s.line, s.col, s.role, s.text, s.qualified) for s in sites)
        files = tuple(sorted({ref.file for ref in found}))
        return ReferencesResult(_node(graph, node_id), len(found), files, found[:limit], len(found) > limit)

    def rename(
        self, node: str, new_name: str, root: str = ".", apply: bool = False, check: bool = True
    ) -> RenameResult:
        """Rename a symbol at every site that resolves to it, across files.

        Works for functions, methods, classes and Clojure vars in every language with a
        tree-sitter grammar. With `apply=True`, the files are written unless a conflict
        blocks the rename: an invalid name, a file that already uses the new name, an override
        or trait method, or a common method name on receivers of unknown type. Without
        `apply`, nothing is written. Strings, comments and keywords stay; `text_mentions`
        counts them. A written rename updates the graph and, with `check`, runs the project
        check: `go vet`, `tsc --noEmit`, `cargo check`, `clj-kondo` or a Python syntax check.

        Args:
            node: The symbol: id, label or a qualified name (`ns/name`, `module.name`, `Class.method`, `crate::m::name`) or `file::name`.
            new_name: The new name, without a namespace or class.
            root: Project directory, relative to the workspace root.
            apply: Write the files when the plan has no conflict.
            check: After a write, run the project check and report it in `check`.
        """
        from graphify import refactor

        path = self._root(root)
        graph = self._graph(path)
        node_id = g.resolve(graph, node)
        plan = refactor.plan_rename(_raw(graph), node_id, new_name, path)
        contents = refactor.rewrite(plan) if plan.edits else {}
        applied = False
        checked = ""
        if apply and not plan.conflicts:
            refactor.apply_rename(plan)
            applied = True
            self._update(path)
            checked = self._check(path, [_relative(path, f) for f in plan.files]) if check else ""
        return RenameResult(
            node=_node(graph, node_id),
            old=plan.old,
            new=new_name,
            applied=applied,
            files=tuple(_relative(path, f) for f in plan.files),
            edits=tuple(RenameEdit(_relative(path, e.file), e.line, e.col, e.old, e.new) for e in plan.edits),
            conflicts=tuple(c.replace(f"{path}/", "") for c in plan.conflicts),
            mentions=tuple(f"{_relative(path, m.file)}:{m.line} {m.text}" for m in plan.mentions if m.kind == "symbol"),
            text_mentions=sum(1 for m in plan.mentions if m.kind != "symbol"),
            diff=_unified_diff(path, contents),
            changes=_changed(path, {f: n for f, (_, n) in contents.items()}, [(e.file, e.line) for e in plan.edits]),
            check=checked,
        )

    def add_parameter(
        self, node: str, parameter: str, argument: str, root: str = ".", apply: bool = False, check: bool = True
    ) -> SignatureResult:
        """Add a parameter to a function or method and pass an argument at every call.

        Finds the calls with the same exact resolution as `references`, so another
        function with the same name stays unchanged. The parameter goes last in the
        definition and the argument goes last in each call. With `apply=True`, the
        files are written unless a conflict blocks the change: a use that is not a call,
        or a Python list that ends with `*args`, `**kwargs` or a default. A written change
        runs the project check, as `rename` does.

        Args:
            node: The function or method: id, label, qualified name or `file::name`.
            parameter: The parameter as the language writes it, such as `currency string`,
                `currency: str` or `currency` (Clojure).
            argument: The value for each call, such as `"EUR"`.
            root: Project directory, relative to the workspace root.
            apply: Write the files when the plan has no conflict.
            check: After a write, run the project check and report it in `check`.
        """
        from graphify import refactor

        from vis_graphify import calls

        path = self._root(root)
        graph = self._graph(path)
        node_id = g.resolve(graph, node)
        sites = refactor.references(_raw(graph), node_id, path)
        old = {site.file: (path / site.file).read_bytes() for site in sites}
        texts = {file: data.decode("utf-8", "replace") for file, data in old.items()}
        plan = calls.plan(sites, parameter, argument, texts)
        new = calls.rewrite(plan, old)
        applied = False
        checked = ""
        if apply and not plan.conflicts:
            for file, data in new.items():
                (path / file).write_bytes(data)
            applied = True
            self._update(path)
            checked = self._check(path, [_relative(path, str(file)) for file in new]) if check else ""
        return SignatureResult(
            node=_node(graph, node_id),
            parameter=parameter,
            argument=argument,
            applied=applied,
            files=tuple(sorted(_relative(path, file) for file in new)),
            edits=tuple(CallEdit(_relative(path, i.file), i.line, i.kind, i.text) for i in plan.inserts),
            conflicts=tuple(c.replace(f"{path}/", "") for c in plan.conflicts),
            diff=_unified_diff(path, {str(path / file): (old[file], data) for file, data in new.items()}),
            changes=_changed(path, new, [(i.file, i.line) for i in plan.inserts]),
            check=checked,
        )

    def move(self, node: str, to: str, root: str = ".", apply: bool = False, check: bool = True) -> MoveResult:
        """Move one top-level definition to another file, which can be new, and fix every use.

        Works for most graphify languages: Clojure, Python, JavaScript, TypeScript, Go, Rust,
        the JVM and .NET languages, PHP, Swift, C and C++, Ruby, Erlang, OCaml, Pascal and more.
        Each import, require and qualified use follows the definition, and the definition takes the imports that
        it needs. Rust also moves `impl` blocks of a moved type and widens private items to
        `pub(crate)` when the new module needs them. With `apply=True`, the files are written unless a conflict blocks the move,
        such as a name that the target file already defines or an import cycle. A written
        move updates the graph and, with `check`, runs the project check, as `rename` does.

        Args:
            node: The definition: id, label, qualified name or `file::name`.
            to: The target file, relative to the project root. For Clojure, also a namespace name.
            root: Project directory, relative to the workspace root.
            apply: Write the files when the plan has no conflict.
            check: After a write, run the project check and report it in `check`.
        """
        from graphify import moves

        path = self._root(root)
        graph = self._graph(path)
        try:
            node_id = g.resolve(graph, node)
        except g.NodeNotFound:
            if "::" not in node:
                raise
            node_id = node
        plan = moves.plan_move(_raw(graph), node_id, to, path)
        return self._moved(path, plan, apply, check)

    def move_file(
        self, path: str, new_path: str, root: str = ".", tests: bool = True, apply: bool = False, check: bool = True
    ) -> MoveResult:
        """Move or rename a source file and rename its module or namespace at every use.

        Works for the same languages as `move`. Imports, requires, `mod` declarations,
        qualified uses and paths in build files follow the file. For Clojure, the `ns` form
        changes, and with `tests` the test namespace `x-test` of namespace `x` moves along.
        With `apply=True`, the files are written unless a conflict blocks the move, such as
        a target file that exists. A written move updates the graph and, with `check`,
        runs the project check, as `rename` does.

        Args:
            path: The file to move, relative to the project root.
            new_path: The new file path, relative to the project root. For Clojure, also a namespace name.
            root: Project directory, relative to the workspace root.
            tests: For Clojure, also move the test namespace of the moved namespace.
            apply: Write the files when the plan has no conflict.
            check: After a write, run the project check and report it in `check`.
        """
        from graphify import moves

        base = self._root(root)
        plan = moves.plan_move_file(_raw(self._graph(base)), path, new_path, base, tests=tests)
        return self._moved(base, plan, apply, check)

    def _moved(self, path: Path, plan, apply: bool, check: bool) -> MoveResult:
        """Write a conflict-free move plan when `apply` is set, then describe it."""
        from graphify import moves

        applied = False
        checked = ""
        if apply and not plan.conflicts:
            moves.apply_move(plan)
            applied = True
            self._update(path)
            kept = [_relative(path, f) for f in plan.files if f not in plan.deleted]
            checked = self._check(path, kept) if check else ""
        return MoveResult(
            subject=plan.subject,
            source=_relative(path, plan.source),
            target=_relative(path, plan.target),
            applied=applied,
            files=tuple(_relative(path, f) for f in plan.files),
            created=tuple(_relative(path, f) for f in plan.created),
            deleted=tuple(_relative(path, f) for f in plan.deleted),
            renames=tuple(f"{old} -> {new}" for old, new in plan.renames),
            conflicts=tuple(c.replace(f"{path}/", "") for c in plan.conflicts),
            notes=tuple(n.replace(f"{path}/", "") for n in plan.notes),
            mentions=tuple(f"{_relative(path, m.file)}:{m.line} {m.text}" for m in plan.mentions if m.kind == "symbol"),
            text_mentions=sum(1 for m in plan.mentions if m.kind != "symbol"),
            diff=_unified_diff(path, plan.contents),
            check=checked,
        )

    def domain(
        self, root: str = ".", plugins: list[str] | None = None, limit: int = 200, timeout_s: int = 600
    ) -> DomainReport:
        """Map the domain flows of a project: commands, queries, events, effects and their schemas.

        Domain plugins read framework registrations, such as edd-core `reg-cmd`
        and `reg-event`, and follow the links across services. Writes
        `<root>/graphify-out/domain/DOMAIN.md` (Mermaid diagrams, flows,
        findings, schemas), `domain.json` and one JSON Schema for each Malli
        schema. Findings report, for example, calls to commands or queries
        that no service registers.

        Args:
            root: Project directory, relative to the workspace root.
            plugins: Use only these plugin specs: a name such as `edd-core`, `module:attr`, or a
                `.py` file or directory. None detects the plugins that apply.
            limit: Maximum count of returned findings. The counts stay complete.
            timeout_s: Stop graphify after this many seconds.
        """
        path = self._root(root)
        argv = graphify_command() + ["domain", str(path), "--json"]
        for spec in plugins or ():
            argv += ["--plugin", spec]
        try:
            done = self._run(argv, cwd=str(path), env=_plugin_env(), capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"graphify domain did not finish in {timeout_s} s for {path}.") from error
        stderr = done.stderr or ""
        try:
            data = json.loads(done.stdout or "")
        except json.JSONDecodeError:
            data = None
        if done.returncode != 0 or not isinstance(data, dict):
            raise RuntimeError(
                f"graphify domain exited with status {done.returncode}.\n{_tail(stderr or done.stdout or '')}"
            )
        raw = data.get("findings") or []
        findings = tuple(
            DomainFinding(
                severity=str(f.get("severity") or ""),
                code=str(f.get("code") or ""),
                message=str(f.get("message") or ""),
                file=str(f.get("file") or ""),
                line=f.get("line") if isinstance(f.get("line"), int) else None,
                element=str(f.get("ref") or ""),
                plugin=str(f.get("plugin") or ""),
            )
            for f in raw[:limit]
        )
        counts = data.get("finding_counts") or {}
        prefix = "warning: plugin error: "
        return DomainReport(
            root=str(data.get("root") or path),
            markdown_path=str(data.get("markdown") or ""),
            json_path=str(data.get("json") or ""),
            plugins=tuple(data.get("plugins") or ()),
            services=tuple(data.get("services") or ()),
            counts=dict(data.get("counts") or {}),
            relations=int(data.get("relations") or 0),
            schemas=int(data.get("schemas") or 0),
            errors=int(counts.get("error") or 0),
            warnings=int(counts.get("warning") or 0),
            notes=int(counts.get("info") or 0),
            findings=findings,
            plugin_errors=tuple(line[len(prefix) :] for line in stderr.splitlines() if line.startswith(prefix)),
            truncated=len(raw) > limit,
        )

    def _kb(self, url: str | None, timeout_s: float = 60.0) -> kb.KBClient:
        """A client for `url`, else the server that kb_serve started, else `$GRAPHIFY_KB_URL`, else the default."""
        target = url or self._kb_url or os.environ.get("GRAPHIFY_KB_URL") or kb.DEFAULT_URL
        return kb.KBClient(
            url=target, token=os.environ.get("GRAPHIFY_KB_TOKEN") or None, actor=self._kb_actor, timeout_s=timeout_s
        )

    @staticmethod
    def _kb_probe(client: kb.KBClient) -> tuple[dict, dict] | None:
        """The health and stats answers of a running server, or None when nothing answers."""
        try:
            health = client.request("GET", "/api/health")
        except kb.KBRequestError:
            return None
        return health, client.request("GET", "/api/stats")

    def kb_serve(
        self, root: str = ".", port: int = 7878, host: str = "127.0.0.1", read_only: bool = False, timeout_s: int = 120
    ) -> kb.KBServer:
        """Start the knowledge base server of a project graph, or use the one that already runs.

        The server is `graphify kb serve`: an HTTP API and an admin UI in the
        browser. People and agents edit the same graph there. Changes are
        undoable and the server writes them to `graphify-out/kb/`. The other
        kb_* methods use this server until you give them another `url`.
        Builds the graph first when it is missing.

        Args:
            root: Project directory, relative to the workspace root.
            port: TCP port of the server.
            host: Interface to bind. Off loopback, the server needs `$GRAPHIFY_KB_TOKEN` with at least 16 characters.
            read_only: Refuse all writes.
            timeout_s: Stop waiting for the server after this many seconds.
        """
        path = self._root(root)
        graph_file = g.graph_path(path)
        if not graph_file.is_file():
            self._update(path)
        url = f"http://{f'[{host}]' if ':' in host else host}:{port}"
        client = self._kb(url)
        running = self._kb_probe(client)
        if running:
            health, stats = running
            served = Path(str(stats.get("graph") or "")).resolve()
            if served != graph_file.resolve():
                raise RuntimeError(f"Port {port} already serves {served}. Choose another port.")
            self._kb_url = url
            process = self._kb_servers.get(url)
            return kb.server(health, stats, url, started=False, pid=process.pid if process else None)
        out = graph_file.parent
        _ignore_output(out)
        log = out / "kb-serve.log"
        argv = graphify_command() + ["kb", "serve", str(graph_file), "--host", host, "--port", str(port)]
        if read_only:
            argv.append("--read-only")
        with open(log, "wb") as handle:
            process = self._spawn(
                argv,
                cwd=str(path),
                env=_child_env(),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + timeout_s
        while (running := self._kb_probe(client)) is None:
            if process.poll() is not None:
                output = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
                raise RuntimeError(f"graphify kb serve exited with status {process.returncode}.\n{_tail(output)}")
            if time.monotonic() > deadline:
                process.terminate()
                raise TimeoutError(f"graphify kb serve did not answer at {url} in {timeout_s} s.")
            time.sleep(0.2)
        self._kb_servers[url] = process
        self._kb_url = url
        return kb.server(*running, url, started=True, pid=process.pid, log=str(log))

    def kb_stop(self, url: str | None = None) -> kb.KBServer:
        """Stop a knowledge base server that kb_serve started. The server writes pending changes first.

        Args:
            url: Server URL. None stops the server that the kb_* methods use.
        """
        client = self._kb(url)
        process = self._kb_servers.get(client.url)
        if process is None:
            raise RuntimeError(f"This extension did not start the server at {client.url}. Stop it where it runs.")
        running = self._kb_probe(client)
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        del self._kb_servers[client.url]
        if self._kb_url == client.url:
            self._kb_url = None
        health, stats = running or ({}, {})
        return dataclasses.replace(kb.server(health, stats, client.url, started=False, pid=process.pid), stopped=True)

    def kb_search(
        self, query: str, url: str | None = None, limit: int = 20, cls: str | None = None
    ) -> kb.KBSearchResult:
        """Search the nodes and documents of the knowledge base by label, id, title and text.

        Args:
            query: Plain words, not a regex.
            url: Server URL. None uses the server of kb_serve.
            limit: Maximum count of matches.
            cls: Return only nodes of this ontology class or its subclasses, such as `concept` or `code`.
        """
        answer = self._kb(url).request("GET", "/api/search", query={"q": query, "limit": limit, "class": cls})
        return kb.search_result(query, answer)

    def kb_node(self, node: str, url: str | None = None, limit: int = 50) -> kb.KBNode:
        """One knowledge base node with its class, attributes and edges.

        Args:
            node: Node id, or an exact label when no node has that id.
            url: Server URL. None uses the server of kb_serve.
            limit: Maximum count of edges in each direction.
        """
        client = self._kb(url)
        try:
            answer = client.request("GET", kb.node_path(node), query={"limit": limit})
        except kb.KBRequestError as error:
            if error.status != 404:
                raise
            answer = client.request("GET", "/api/nodes", query={"label": node})
        return kb.node_result(answer)

    def kb_write(self, ops: list[dict], url: str | None = None) -> kb.KBChange:
        """Apply operations to the knowledge base as one undoable change. One failure applies nothing.

        Each operation is a dict with an `op` key:
          {"op": "add_node", "label": ..., "class": ..., "id": ..., "attrs": {...}}
          {"op": "update_node", "id": ..., "attrs": {...}}   a None value removes the attribute
          {"op": "delete_node", "id": ...}                  also removes its edges
          {"op": "add_edge", "source": ..., "target": ..., "relation": ..., "confidence": ..., "attrs": {...}}
          {"op": "update_edge" | "delete_edge", "id": ...} or with source, target and relation
          {"op": "add_document", "title": ..., "body": markdown, "tags": [...], "nodes": [ids]}
          {"op": "update_document", "id": ..., "title" | "body" | "tags" | "nodes": ...}
          {"op": "delete_document", "id": ...}
        A `[[label]]` in a document body links the document to that node.

        Args:
            ops: The operations, in order. At most 10000.
            url: Server URL. None uses the server of kb_serve.
        """
        if not isinstance(ops, (list, tuple)) or not ops:
            raise ValueError("ops must be a non-empty list of operation dicts.")
        answer = self._kb(url).request("POST", "/api/batch", {"ops": list(ops)})
        names = Counter(str(op.get("op")) for op in ops if isinstance(op, dict))
        summary = ", ".join(f"{count} {name}" for name, count in names.items())
        return kb.change_result(f"Applied {summary}", answer)

    def kb_ingest(
        self,
        source: str,
        url: str | None = None,
        mode: str = "text",
        title: str | None = None,
        tags: list[str] | None = None,
        link: bool = True,
        text: str | None = None,
    ) -> kb.KBIngestResult:
        """Add a file, a web page or text to the knowledge base as a document.

        Markdown and text stay as they are. PDF, HTML, images and other files
        become attachments with extracted text when the server can read it.
        `text` mode links the document to the nodes that it names, without a
        model. `llm` mode also asks the graphify model backend for new nodes
        and edges.

        Args:
            source: A file path relative to the workspace root, an http(s) URL, or the document name when `text` is set.
            url: Server URL. None uses the server of kb_serve.
            mode: `text` or `llm`.
            title: Document title. None uses the file name or the first heading.
            tags: Document tags.
            link: Link the document to the existing nodes that it names.
            text: Document content. Then `source` is only its name.
        """
        if mode not in ("text", "llm"):
            raise ValueError("mode must be 'text' or 'llm'.")
        body: dict = {"mode": mode, "title": title, "tags": list(tags) if tags else None, "link": link}
        if text is not None:
            body.update(name=source, text=text)
        elif source.startswith(("http://", "https://")):
            body.update(name=source.rstrip("/").rsplit("/", 1)[-1] or "page", url=source)
        else:
            file = Path(source).expanduser()
            if not file.is_absolute():
                file = Path(self._workspace_root()) / file
            if not file.is_file():
                raise FileNotFoundError(f"{file} is not a file.")
            body.update(name=file.name, data_base64=base64.b64encode(file.read_bytes()).decode())
        answer = self._kb(url, 900.0 if mode == "llm" else 120.0).request("POST", "/api/ingest", body)
        return kb.ingest_result(answer)

    def kb_validate(self, url: str | None = None, limit: int = 50, info: bool = False) -> kb.KBReport:
        """Check the knowledge base against its ontology: missing properties, bad edge ends, orphans, cycles.

        Args:
            url: Server URL. None uses the server of kb_serve.
            limit: Maximum count of returned findings. The counts stay complete.
            info: Also return info findings, such as orphan nodes.
        """
        query = {"limit": limit, "info": "1" if info else "0"}
        return kb.report_result(self._kb(url).request("GET", "/api/validate", query=query))

    def kb_undo(self, url: str | None = None, seq: int | None = None) -> kb.KBChange:
        """Revert the latest change, or the change with history number `seq`.

        Args:
            url: Server URL. None uses the server of kb_serve.
            seq: History number of the change. None reverts the latest change that is not reverted yet.
        """
        answer = self._kb(url).request("POST", "/api/undo", {"seq": seq} if seq is not None else {})
        return kb.change_result("Undo", answer)

    def kb_publish(self, url: str | None = None, path: str | None = None) -> kb.KBPublishResult:
        """Write a static HTML view of the knowledge base that opens without a server.

        Args:
            url: Server URL. None uses the server of kb_serve.
            path: An `.html` file name inside `graphify-out/`. None writes `graphify-out/kb.html`.
        """
        answer = self._kb(url).request("POST", "/api/publish", {"path": path} if path else {})
        return kb.KBPublishResult(path=str(answer.get("path") or ""), bytes=int(answer.get("bytes") or 0))
