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
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Callable
from urllib.parse import urlsplit

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
    at: Annotated[str, "Where the edge occurs, as `file:Lline`. Empty for knowledge."]
    knowledge: Annotated[bool, "True for an edge of the knowledge, such as a note that mentions code."] = False

    def __str__(self) -> str:
        return f"{self.relation} {self.node}" + (" (knowledge)" if self.knowledge else "")


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
    """Code nodes, and knowledge nodes and documents, that match a pattern."""

    pattern: Annotated[str, "The pattern: a case-insensitive regex for code, plain words for knowledge."]
    total: Annotated[int, "Count of all code matches."]
    nodes: Annotated[tuple[Node, ...], "The best code matches, labels first, then by degree."]
    knowledge: Annotated[
        tuple[kb.KBHit, ...], "Knowledge nodes and documents that match, best first. Code nodes stay in `nodes`."
    ] = ()
    source: Annotated[str, "The knowledge source that answered: `local`, a folder or a server URL, else empty."] = ""
    note: Annotated[str, "Why the knowledge part is missing, such as a server that does not answer, else empty."] = ""

    def __str__(self) -> str:
        lines = []
        if self.nodes or not self.knowledge:
            lines.append(
                _listing(f"{len(self.nodes)} of {self.total} code nodes match {self.pattern!r}:", self.nodes, "nodes")
            )
        if self.knowledge:
            lines.append(
                _listing(f"{len(self.knowledge)} knowledge matches in {self.source}:", self.knowledge, "knowledge")
            )
        if self.note:
            lines.append(self.note)
        return "\n".join(lines)


@dataclass(frozen=True)
class Explanation:
    """One node with its direct neighbours: a code node, or a knowledge node or document."""

    node: Annotated[Node, "The node that was asked about. A knowledge node has its ontology class as `kind`."]
    outgoing: Annotated[tuple[Edge, ...], "Edges from the node: what it calls, references, contains or links."]
    incoming: Annotated[tuple[Edge, ...], "Edges to the node: callers, importers, its container, notes about it."]
    truncated: Annotated[bool, "True when `limit` cut either list."]
    attributes: Annotated[
        dict[str, Any], "Stored attributes of a knowledge node, such as the text of a document. Empty for code."
    ] = dataclasses.field(default_factory=dict)
    source: Annotated[str, "The knowledge source that answered: `local`, a folder or a server URL, else empty."] = ""
    note: Annotated[str, "Why the knowledge part is missing, such as a server that does not answer, else empty."] = ""

    def __str__(self) -> str:
        lines = [str(self.node)]
        if self.attributes:
            text = ", ".join(f"{key}={value!r}" for key, value in self.attributes.items())
            lines.append(f"Attributes: {text if len(text) <= 600 else text[:599] + '…'}")
        lines += [_listing("Uses:", self.outgoing, "outgoing"), _listing("Used by:", self.incoming, "incoming")]
        if self.note:
            lines.append(self.note)
        return "\n".join(lines)


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
    moved_nodes: Annotated[tuple[str, ...], "`old id -> new id` of each graph node that the write gave a new id."] = ()
    kb: Annotated[
        tuple[str, ...],
        "One line for each knowledge base that got the new node ids: the project store or a server, with the counts or the error.",
    ] = ()

    def __str__(self) -> str:
        """A short summary: counts, conflicts, each changed line, code mentions and the knowledge base."""
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
        lines += [f"Knowledge base {line}" for line in self.kb]
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
    moved_nodes: Annotated[tuple[str, ...], "`old id -> new id` of each graph node that the write gave a new id."] = ()
    kb: Annotated[
        tuple[str, ...],
        "One line for each knowledge base that got the new node ids: the project store or a server, with the counts or the error.",
    ] = ()

    def __str__(self) -> str:
        """A short summary: the move, conflicts, renames, notes, code mentions, the diff and the knowledge base."""
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
        lines += [f"Knowledge base {line}" for line in self.kb]
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


def _move_maps(root: Path, plan) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """The moved files, the renamed modules and the changed files of a move plan, for `graph.moved_ids`.

    A move that also moves a test file deletes and creates one more file each: that pair moves too.
    Else a deleted file pairs with the one created file with its name.
    """
    source, target = _relative(root, plan.source), _relative(root, plan.target)
    files = {source: target}
    deleted = [file for file in (_relative(root, f) for f in plan.deleted) if file != source]
    created = [file for file in (_relative(root, f) for f in plan.created) if file != target]
    if len(deleted) == 1 and len(created) == 1:
        files[deleted[0]] = created[0]
    else:
        for old in deleted:
            same = [new for new in created if Path(new).name == Path(old).name]
            if len(same) == 1:
                files[old] = same[0]
    return files, dict(plan.renames), {_relative(root, f) for f in plan.files} | set(files)


KB_BATCH_LIMIT = 10000
"""The most operations that one knowledge base batch takes."""

KB_MOVE_PAIRS = 1000
"""The most id pairs in one `graphify kb move` command line."""


def _kb_outcome(place: str, results: list, error: str = "") -> str:
    """One line on the node moves that a knowledge base got: the count of each status, then the error."""
    statuses = Counter(str(item.get("status")) for item in results if isinstance(item, dict))
    counts = (
        f"{statuses['moved']} moved, {statuses['pending']} pending, {statuses['skipped']} with no records at the old id"
    )
    if not error:
        return f"{place}: {counts}"
    return f"{place}: {counts}, then not updated: {error}" if results else f"{place}: not updated: {error}"


KB_REGISTRY = "kb-serve.json"
"""The file in the output folder of a store that names the server that this extension started for it."""

KB_SNAPSHOTS = "snapshots"
"""The folder in `graphify-out/` that holds the snapshots of knowledge sources."""

KB_SNAPSHOT_FILE = "snapshot.json"
"""The file in a snapshot folder that names the copied source and the time of the copy."""

KB_CHECK_SECONDS = 30.0
"""A store server that answered for its store within this time is used without a new check."""

KB_EXPORT_FORMAT = "graphify-kb-export"
"""The `format` of a `GET /api/export` answer. graphify v0.9.93-blockether.17 added that route."""


@dataclass
class _KBSession:
    """The knowledge state of one Vis session.

    `servers` maps each server URL that this session checked to its base URL after a redirect. `tokens` holds
    the checked token of each base URL. `changes` holds the history numbers of the own changes of each
    source, oldest first, so that undo reverts only them.
    """

    servers: dict[str, str] = dataclasses.field(default_factory=dict)
    tokens: dict[str, str] = dataclasses.field(default_factory=dict, repr=False)
    changes: dict[str, list[tuple[int, ...]]] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class _KBSource:
    """One resolved knowledge source.

    `kind` is `local` (the store of the workspace root), `store` (another store on this machine),
    `snapshot` (a copy from snapshot) or `server` (a server URL). A store has its `graph` file,
    a server its `url`. `key` names the source in the session state.
    """

    name: str
    kind: str
    key: str
    graph: Path | None = None
    url: str = ""


def _kb_slug(text: str) -> str:
    """A folder name: lower-case letters, digits, dots, dashes and underscores."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", str(text).lower()).strip("-.")
    return slug[:80] or "source"


def _kb_snapshot_name(source: _KBSource) -> str:
    """The default snapshot name: the host, port and path of a server, else the project folder of a store."""
    if source.graph is None:
        parts = urlsplit(source.url)
        return _kb_slug("-".join(p for p in (parts.hostname or "", str(parts.port or ""), parts.path.strip("/")) if p))
    out = source.graph.parent
    return _kb_slug(out.parent.name if out.name == g.GRAPH_DIR else out.name)


def _kb_store_graph(root: Path, text: str) -> Path:
    """The graph.json of a store path: a project folder, its graphify-out folder, a snapshot folder or a file."""
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    candidates = [path] if path.suffix == ".json" else [path / g.GRAPH_DIR / g.GRAPH_FILE, path / g.GRAPH_FILE]
    for graph in candidates:
        if graph.is_file():
            return graph
    raise FileNotFoundError(
        f"{text} is not a knowledge store: it has no graph.json. Give `local`, a project or snapshot folder, "
        "a graph.json, or a server URL that starts with http:// or https://."
    )


def _kb_registry(graph: Path) -> dict | None:
    """The entry of the server that this extension started for a store, or None."""
    try:
        data = json.loads((graph.parent / KB_REGISTRY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("url"), str) else None


def _kb_store_saved(graph: Path) -> bool:
    """True when the store of a graph has saved edits: `kb/store.sqlite`, or the `kb/overlay.json` of an import.

    graphify moves `kb/overlay.json` into `kb/store.sqlite` when it next opens the store.
    """
    store = graph.parent / "kb"
    return (store / "store.sqlite").is_file() or (store / "overlay.json").is_file()


def _kb_has_knowledge(graph: Path) -> bool:
    """True when the store of a graph holds knowledge: saved edits, documents, or a running server.

    A server of this extension can hold a change that it did not save yet, so its store counts too.
    """
    saved = _kb_store_saved(graph) or (graph.parent / "kb" / "documents").is_dir()
    return saved or (graph.parent / KB_REGISTRY).is_file()


def _kb_write_json(path: Path, data) -> None:
    """Write a JSON file in one step, so that a reader gets the old file or the new one."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _kb_free_port(host: str) -> int:
    """A TCP port that is free on `host` now."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _kb_alive(pid: int) -> bool:
    """True when a process with this id runs. An exited child of this process is reaped, so it does not count."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kb_store_token(url: str) -> str | None:
    """The token of a store server of this extension: none on loopback, else `$GRAPHIFY_KB_TOKEN`."""
    return None if kb.is_loopback_url(url) else os.environ.get("GRAPHIFY_KB_TOKEN", "").strip() or None


def _kb_write_export(folder: Path, answer: dict) -> dict[str, int]:
    """Write the files of a `/api/export` answer into `folder`. Return the counts of the copy.

    Each path must stay inside `folder`. Raises KBRequestError for an answer that is not an export.
    """
    files = answer.get("files")
    if answer.get("format") != KB_EXPORT_FORMAT or not isinstance(files, list):
        raise kb.KBRequestError("The server sent no store export. It needs graphify v0.9.93-blockether.17 or later.")
    counts = {"nodes": 0, "edges": 0, "documents": 0, "assets": 0}
    for item in files:
        name = str(item.get("path") or "") if isinstance(item, dict) else ""
        parts = PurePosixPath(name).parts
        if not parts or name.startswith("/") or "\\" in name or ":" in name or ".." in parts:
            raise kb.KBRequestError(f"The export names an unsafe path: {name!r}.")
        target = folder.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if "json" in item:
            target.write_text(json.dumps(item["json"], ensure_ascii=False), encoding="utf-8")
        elif "text" in item:
            target.write_text(str(item["text"]), encoding="utf-8")
        elif "base64" in item:
            target.write_bytes(base64.b64decode(str(item["base64"])))
            counts["assets"] += 1
        else:
            raise kb.KBRequestError(f"The export has no content for {name}.")
        if name == g.GRAPH_FILE and isinstance(item.get("json"), dict):
            counts["nodes"] = len(item["json"].get("nodes") or ())
            counts["edges"] = len(item["json"].get("links", item["json"].get("edges")) or ())
        elif parts[:2] == ("kb", "documents") and len(parts) == 4 and parts[3] == "meta.json":
            counts["documents"] += 1
    if not (folder / g.GRAPH_FILE).is_file():
        raise kb.KBRequestError("The export has no graph.json.")
    return counts


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


def _end(graph: g.Graph, node_id: str, label: str) -> Node:
    """The node at the other end of a knowledge edge. A code node keeps its file and line."""
    return (
        _node(graph, node_id)
        if node_id in graph.nodes
        else Node(node_id, label or node_id, "", None, "knowledge", None)
    )


def _kb_explanation(graph: g.Graph, known: kb.KBNode, limit: int) -> Explanation:
    """An Explanation of a knowledge node, with all its edges."""
    file = str(known.attributes.get("source_file") or "")
    edges = {
        direction: tuple(
            Edge(edge.relation, _end(graph, edge.other, edge.other_label), edge.confidence, "", knowledge=edge.kb)
            for edge in known.edges
            if edge.direction == direction
        )
        for direction in ("out", "in")
    }
    return Explanation(
        node=Node(known.id, known.label, "" if file == kb.SOURCE_FILE else file, None, known.cls, None),
        outgoing=edges["out"],
        incoming=edges["in"],
        truncated=known.out_total > limit or known.in_total > limit,
        attributes=dict(known.attributes),
        source=known.source,
    )


KB_BODY_CHARS = 8000
"""The longest document text in an explanation. A longer text ends there, and `body_chars` gives its length."""


def _document_explanation(graph: g.Graph, answer: dict, source: str) -> Explanation:
    """An Explanation of a knowledge document: its title, tags and text, and the nodes that it links."""
    doc_id = str(answer.get("id") or "")
    attributes = {key: answer[key] for key in ("title", "tags", "source", "updated_at") if answer.get(key)}
    body = str(answer.get("body") or "")
    if body:
        attributes["body"] = body[:KB_BODY_CHARS]
    if len(body) > KB_BODY_CHARS:
        attributes["body_chars"] = len(body)
    linked = [item for item in answer.get("linked") or () if isinstance(item, dict) and item.get("id")]
    return Explanation(
        node=Node(doc_id, str(answer.get("title") or doc_id), "", None, "document", None),
        outgoing=tuple(
            Edge("links", _end(graph, str(item["id"]), str(item.get("label") or "")), "EXTRACTED", "", knowledge=True)
            for item in linked
        ),
        incoming=(),
        truncated=False,
        attributes=attributes,
        source=source,
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


def _moved_base(base: str, error: kb.KBRequestError) -> str | None:
    """The new base URL when the health check redirects on the same host, such as http to https, else None.

    A move to another host or from https to http stays an error: the token must not follow it.
    """
    suffix = "/api/health"
    if not 300 <= error.status < 400 or not error.location.endswith(suffix):
        return None
    old, new = urlsplit(base), urlsplit(error.location)
    if new.hostname != old.hostname or (old.scheme == "https" and new.scheme != "https"):
        return None
    return kb.normalize_url(error.location[: -len(suffix)])


def _local_host(host: str) -> bool:
    """True when this machine can listen on `host`: a loopback, unspecified or own address, or a name of one."""
    try:
        found = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    for family, kind, proto, _, address in found:
        try:
            with socket.socket(family, kind, proto) as probe:
                probe.bind((address[0], 0, *address[2:]))
        except OSError:
            continue
        return True
    return False


class GraphifyTools:
    """Build and query a graphify knowledge graph of the workspace code."""

    def __init__(
        self,
        workspace_root: Callable[[], str] | None = None,
        runner: Runner | None = None,
        checker: Callable[[Path, list[str]], str] | None = None,
        spawn: Callable[..., subprocess.Popen] | None = None,
        kb_actor: str = "agent",
        kb_ask_token: Callable[[str, Callable[[str], str | None]], str | None] | None = None,
        kb_settings: Callable[[], tuple[str, str]] | None = None,
        kb_session: Callable[[], str] | None = None,
    ):
        self._workspace_root = workspace_root or os.getcwd
        self._run = runner or subprocess.run
        self._check = checker or project_check
        self._spawn = spawn or subprocess.Popen
        self._kb_actor = kb_actor
        # One gateway serves many sessions: each session key gets its own source, tokens and changes.
        self._kb_session = kb_session
        self._kb_sessions: dict[str, _KBSession] = {}
        # The store servers that this extension started, and the time of the latest check, by store key.
        self._kb_procs: dict[str, subprocess.Popen] = {}
        self._kb_checked: dict[str, tuple[str, float]] = {}
        self._kb_lock = threading.RLock()
        # The masked form that asks the person for a server token.
        self._kb_ask_token = kb_ask_token
        # The server and token text that the person saved in Settings; empty text means not set.
        self._kb_settings = kb_settings

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

    def search(
        self, pattern: str, root: str = ".", limit: int = 25, source: str | None = None, cls: str | None = None
    ) -> SearchResult:
        """Find code nodes, and knowledge nodes and documents, that match a pattern.

        Code matches by label, id or file with a case-insensitive regex. Knowledge matches by the words in
        labels, titles and text. A graph without knowledge gives only code, and starts no server.

        Args:
            pattern: Regex, for example `run-tests` or `gateway/.*client`. An invalid regex, such as `*out*`, matches as text.
            root: Project directory, relative to the workspace root.
            limit: Maximum count of code nodes, and of knowledge matches.
            source: Where the knowledge is: `local`, a store or snapshot folder, or a server URL. None uses the
                configured server, else the store of the graph.
            cls: Return only knowledge nodes of this ontology class or its subclasses, such as `concept`. Then
                code nodes stay out.
        """
        path = self._root(root)
        graph = self._graph(path)
        ids, total = ([], 0) if cls else g.search(graph, pattern, limit)
        nodes = tuple(_node(graph, node_id) for node_id in ids)
        target, note = self._kb_knowledge(path, source)
        if target is None:
            return SearchResult(pattern, total, nodes, note=note)
        query = {"q": pattern, "limit": limit, "class": cls}
        try:
            answer, _, target = self._kb_call(target, "GET", "/api/search", query=query)
        except (kb.KBRequestError, OSError, RuntimeError) as error:
            if source:
                raise
            return SearchResult(
                pattern, total, nodes, source=target.name, note=f"No knowledge from {target.name}: {error}"
            )
        # The store also holds the code nodes of its graph; those already are in `nodes`.
        hits = tuple(hit for hit in kb.search_hits(answer) if hit.kind == "document" or hit.id not in graph.nodes)
        return SearchResult(pattern, total, nodes, knowledge=hits, source=target.name)

    def explain(self, node: str, root: str = ".", limit: int = 40, source: str | None = None) -> Explanation:
        """Show one node with its direct neighbours: what it calls and what uses it, or a knowledge node or document.

        A code node also gets the knowledge edges that reach it, such as a note that mentions it. A name
        that no code node has goes to the knowledge.

        Args:
            node: Node id, label (`run-tests()`), bare name or a qualified name (`ns/name`, `module.name`, `Class.method`, `crate::m::name`) or `file::name`.
                For knowledge, a node id, a document id or an exact label.
            root: Project directory, relative to the workspace root.
            limit: Maximum count of edges in each direction.
            source: Where the knowledge is: `local`, a store or snapshot folder, or a server URL. None uses the
                configured server, else the store of the graph.
        """
        path = self._root(root)
        graph = self._graph(path)
        try:
            node_id = g.resolve(graph, node)
        except g.NodeNotFound:
            target, _ = self._kb_knowledge(path, source)
            if target is None:
                raise
            try:
                return self._kb_explain(target, graph, node, limit)
            except kb.KBRequestError as error:
                if error.status == 404:
                    raise g.NodeNotFound(
                        f"No code node and nothing in {target.name} matches {node!r}. Try graphify.search()."
                    ) from None
                raise g.NodeNotFound(
                    f"No code node matches {node!r}, and {target.name} did not answer: {error}"
                ) from None
        out = graph.outgoing.get(node_id, [])
        inc = graph.incoming.get(node_id, [])
        outgoing = [_edge(graph, link, link.target) for link in out[:limit]]
        incoming = [_edge(graph, link, link.source) for link in inc[:limit]]
        truncated = len(out) > limit or len(inc) > limit
        target, note = self._kb_knowledge(path, source)
        name = ""
        if target is not None:
            name = target.name
            try:
                answer, _, target = self._kb_call(target, "GET", kb.node_path(node_id), query={"limit": limit})
            except (kb.KBRequestError, OSError, RuntimeError) as error:
                # 404: the knowledge has no record of this code node.
                missing = isinstance(error, kb.KBRequestError) and error.status == 404
                if source and not missing:
                    raise
                note = "" if missing else f"No knowledge from {target.name}: {error}"
            else:
                known = _kb_explanation(graph, kb.node_result(answer, source=target.name), limit)
                outgoing += [edge for edge in known.outgoing if edge.knowledge]
                incoming += [edge for edge in known.incoming if edge.knowledge]
                truncated = truncated or known.truncated
        return Explanation(
            node=_node(graph, node_id),
            outgoing=tuple(outgoing),
            incoming=tuple(incoming),
            truncated=truncated,
            source=name,
            note=note,
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
        When a node gets a new id, its knowledge base records follow it, and `kb` reports
        the outcome. The project store `graphify-out/kb/` gets the new ids, and so does the
        configured server in Settings or `$GRAPHIFY_KB_URL`.

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
        moved: dict[str, str] = {}
        told: tuple[str, ...] = ()
        if apply and not plan.conflicts:
            refactor.apply_rename(plan)
            applied = True
            self._update(path)
            scope = {_relative(path, f) for f in plan.files}
            moved = g.moved_ids(graph, g.load(path), {}, {plan.old: new_name}, scope)
            told = self._kb_moves(path, moved)
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
            moved_nodes=tuple(f"{old} -> {new}" for old, new in moved.items()),
            kb=told,
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
        The knowledge base records of a node with a new id follow it, as with `rename`.

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
        return self._moved(path, graph, plan, apply, check)

    def move_file(
        self, path: str, new_path: str, root: str = ".", tests: bool = True, apply: bool = False, check: bool = True
    ) -> MoveResult:
        """Move or rename a source file and rename its module or namespace at every use.

        Works for the same languages as `move`. Imports, requires, `mod` declarations,
        qualified uses and paths in build files follow the file. For Clojure, the `ns` form
        changes, and with `tests` the test namespace `x-test` of namespace `x` moves along.
        With `apply=True`, the files are written unless a conflict blocks the move, such as
        a target file that exists. A written move updates the graph and, with `check`,
        runs the project check, as `rename` does. The knowledge base records of the nodes
        with new ids follow them, as with `rename`.

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
        graph = self._graph(base)
        plan = moves.plan_move_file(_raw(graph), path, new_path, base, tests=tests)
        return self._moved(base, graph, plan, apply, check)

    def _moved(self, path: Path, graph: g.Graph, plan, apply: bool, check: bool) -> MoveResult:
        """Write a conflict-free move plan when `apply` is set, then describe it. `graph` is the graph before."""
        from graphify import moves

        applied = False
        checked = ""
        moved: dict[str, str] = {}
        told: tuple[str, ...] = ()
        if apply and not plan.conflicts:
            moves.apply_move(plan)
            applied = True
            self._update(path)
            moved = g.moved_ids(graph, g.load(path), *_move_maps(path, plan))
            told = self._kb_moves(path, moved)
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
            moved_nodes=tuple(f"{old} -> {new}" for old, new in moved.items()),
            kb=told,
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

    # -- knowledge -------------------------------------------------------------
    # Knowledge lives in a source: `local` (the store of the workspace root), another store or a snapshot
    # on this machine, or a server URL. Each knowledge call takes `source`; None is the configured server,
    # else `local`. A store gets a `graphify kb serve` of this extension, so every source answers over one
    # HTTP API.

    def _kb_knowledge(self, path: Path, source: str | None) -> tuple[_KBSource | None, str]:
        """The knowledge source of search and explain, else None with the reason when there is one.

        A given source always counts, and so does the configured server. Else the store of the graph counts
        when it holds knowledge, so a graph without knowledge starts no server.
        """
        if source:
            return self._kb_source(source), ""
        try:
            configured = self._kb_configured()[0]
        except ValueError as error:
            return None, f"No knowledge: {error}"
        if configured:
            return self._kb_source(configured), ""
        graph = g.graph_path(path).resolve()
        return (self._kb_source(str(graph)), "") if _kb_has_knowledge(graph) else (None, "")

    def _kb_explain(self, target: _KBSource, graph: g.Graph, node: str, limit: int) -> Explanation:
        """A knowledge node or document as an Explanation: by node id, document id, then exact label.

        Raises KBRequestError with status 404 when the source has none of them.
        """
        lookups = (
            (kb.node_path(node), {"limit": limit}),
            (kb.document_path(node), None),
            ("/api/nodes", {"label": node}),
        )
        for path, query in lookups:
            try:
                answer, _, target = self._kb_call(target, "GET", path, query=query)
            except kb.KBRequestError as error:
                if error.status == 404:
                    continue
                raise
            if query is None:
                return _document_explanation(graph, answer, target.name)
            return _kb_explanation(graph, kb.node_result(answer, source=target.name), limit)
        raise kb.KBRequestError(f"{target.name} has no node or document {node!r}.", 404)

    def _kb_state(self) -> _KBSession:
        """The knowledge state of the calling session. Outside Vis, one state serves all calls."""
        key = self._kb_session() if self._kb_session else ""
        with self._kb_lock:
            return self._kb_sessions.setdefault(key, _KBSession())

    def _kb_configured(self, strict: bool = True) -> tuple[str | None, str | None, str]:
        """The configured server, its token and the place of both: `Settings`, `GRAPHIFY_KB_URL` or empty.

        The server in Settings wins over `$GRAPHIFY_KB_URL`. A token goes only with the server of its own
        place: a project file that changes `$GRAPHIFY_KB_URL` never gets the token from Settings.
        An invalid server raises ValueError. With `strict=False`, it counts as no configured server, so that
        it blocks only the calls that use it.
        """
        saved_url, saved_token = self._kb_settings() if self._kb_settings else ("", "")
        place, text, token = "Settings", saved_url.strip(), saved_token
        if not text:
            place, text = "GRAPHIFY_KB_URL", os.environ.get("GRAPHIFY_KB_URL", "").strip()
            token = os.environ.get("GRAPHIFY_KB_TOKEN", "")
        if not text:
            return None, None, ""
        try:
            url = kb.normalize_url(text)
        except ValueError as error:
            if not strict:
                return None, None, ""
            raise ValueError(f"{'graphify_kb_url' if place == 'Settings' else place}: {error}") from None
        return url, token.strip() or None, place

    def _kb_source(self, source: str | None = None) -> _KBSource:
        """Resolve a source. None is the configured server, else `local`.

        Only a text with http:// or https:// is a server. Any other text is a path, relative to the workspace root.
        """
        text = str(source or "").strip() or self._kb_configured()[0] or "local"
        if text.lower().startswith(("http://", "https://")):
            url = kb.normalize_url(text)
            return _KBSource(name=url, kind="server", key=url, url=url)
        if "://" in text:
            raise ValueError(f"{text} is not an http or https URL.")
        root = Path(self._workspace_root()).resolve()
        local = g.graph_path(root).resolve()
        graph = local if text == "local" else _kb_store_graph(root, text)
        if graph == local:
            return _KBSource(name="local", kind="local", key=str(local), graph=local)
        kind = "snapshot" if (graph.parent / KB_SNAPSHOT_FILE).is_file() else "store"
        name = os.path.relpath(graph.parent if graph.name == g.GRAPH_FILE else graph, root)
        return _KBSource(name=name, kind=kind, key=str(graph), graph=graph)

    def _kb_token(self, url: str) -> str | None:
        """The token for a server: the one that this session checked, else the configured one for its server."""
        token = self._kb_state().tokens.get(url)
        if token:
            return token
        configured, configured_token, _ = self._kb_configured(strict=False)
        return configured_token if url == configured else None

    def _kb_client(self, source: _KBSource, timeout_s: float = 60.0) -> kb.KBClient:
        """A client for a source. A store gets its server first: the running one, else a new one.

        A server gets one check in each session, which follows a redirect on the same host. When it needs a
        token that this session does not have, a masked form asks the person for it.
        """
        if source.graph is None:
            state = self._kb_state()
            base = state.servers.get(source.url)
            if base is None:
                base = self._kb_connect(source.url, min(timeout_s, 30.0))
                state.servers[source.url] = base
            return kb.KBClient(url=base, token=self._kb_token(base), actor=self._kb_actor, timeout_s=timeout_s)
        url = self._kb_store_url(source)
        return kb.KBClient(url=url, token=_kb_store_token(url), actor=self._kb_actor, timeout_s=timeout_s)

    def _kb_call(
        self,
        source: str | _KBSource | None,
        method: str,
        path: str,
        body: dict | None = None,
        query: dict | None = None,
        timeout_s: float = 60.0,
    ) -> tuple[dict, kb.KBClient, _KBSource]:
        """Send one request to a source. Returns the answer, the client and the resolved source.

        When the server of a store stops answering, the next call checks it again and starts a new one.
        A read runs once more at once. A write does not, because it can have reached the server. A server
        that refuses the token of this session gets a new check, so the form can ask again, and the call runs
        once more: a refused call changes nothing.
        """
        target = source if isinstance(source, _KBSource) else self._kb_source(source)
        if method != "GET" and target.kind == "snapshot":
            raise ValueError(
                f"{target.name} is a read-only snapshot. Write to the source that it copies, "
                "then copy that source again with graphify.snapshot."
            )
        client = self._kb_client(target, timeout_s)
        try:
            return client.request(method, path, body, query), client, target
        except kb.KBRequestError as error:
            if target.graph is None:
                if error.status != 401:
                    raise
                state = self._kb_state()
                state.tokens.pop(client.url, None)
                state.servers.pop(target.url, None)
            elif error.status:
                raise
            else:
                self._kb_checked.pop(target.key, None)
                if method != "GET":
                    raise kb.KBRequestError(
                        f"The server of {target.name} stopped during {method} {path}, so the change can be "
                        "applied or not. Check it with graphify.search or graphify.explain before you send it "
                        "again. The next call starts a new server."
                    ) from None
        client = self._kb_client(target, timeout_s)
        return client.request(method, path, body, query), client, target

    def _kb_serves(self, url: str, graph: Path) -> tuple[dict, dict] | None:
        """The health and stats answers when the server at `url` serves `graph`, else None."""
        client = kb.KBClient(url=url, token=_kb_store_token(url), actor=self._kb_actor, timeout_s=5.0)
        try:
            health = client.request("GET", "/api/health")
            stats = client.request("GET", "/api/stats")
        except kb.KBRequestError:
            return None
        served = str(stats.get("graph") or "")
        return (health, stats) if served and Path(served).resolve() == graph else None

    def _kb_running(self, source: _KBSource) -> tuple[str, dict, dict] | None:
        """The URL, health and stats of the running server of this extension for a store, else None."""
        assert source.graph is not None
        entry = _kb_registry(source.graph)
        running = self._kb_serves(entry["url"], source.graph) if entry else None
        if entry is None or running is None:
            return None
        self._kb_checked[source.key] = (entry["url"], time.monotonic())
        return entry["url"], *running

    def _kb_store_url(self, source: _KBSource) -> str:
        """The URL of the server of a store. Starts one when none runs."""
        with self._kb_lock:
            checked = self._kb_checked.get(source.key)
            if checked and time.monotonic() - checked[1] < KB_CHECK_SECONDS:
                return checked[0]
            running = self._kb_running(source)
            return running[0] if running else self._kb_start(source)[0]

    def _kb_start(
        self,
        source: _KBSource,
        port: int | None = None,
        host: str = "127.0.0.1",
        read_only: bool = False,
        timeout_s: float = 120.0,
    ) -> tuple[str, dict, dict, str]:
        """Start `graphify kb serve` for a store. Returns the URL, the health and stats answers and the log path.

        The entry `kb-serve.json` in the output folder of the store names the server. The server and the entry
        outlive the gateway, so a later session uses the same server. A snapshot gets a read-only server.
        Builds the graph of the workspace root first when it is missing.
        """
        graph = source.graph
        assert graph is not None
        out = graph.parent
        if source.kind == "local" and not graph.is_file():
            self._update(out.parent)
        _ignore_output(out)
        chosen = port or _kb_free_port(host)
        url = kb.normalize_url(f"http://{f'[{host}]' if ':' in host else host}:{chosen}")
        if port:
            try:
                served = kb.KBClient(url=url, timeout_s=5.0).request("GET", "/api/stats").get("graph") or "a server"
            except kb.KBRequestError as error:
                served = "a server" if error.status else None
            if served:
                raise RuntimeError(f"Port {port} already serves {served}. Choose another port.")
        read_only = read_only or source.kind == "snapshot"
        log = out / "kb-serve.log"
        argv = graphify_command() + ["kb", "serve", str(graph), "--host", host, "--port", str(chosen)]
        if read_only:
            argv.append("--read-only")
        env = _child_env()
        if kb.is_loopback_url(url):
            # A loopback server needs no token, so the token of a hosted server stays out of it.
            env.pop("GRAPHIFY_KB_TOKEN", None)
        with open(log, "wb") as handle:
            process = self._spawn(
                argv,
                cwd=str(out.parent if out.name == g.GRAPH_DIR else out),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + timeout_s
        while (running := self._kb_serves(url, graph)) is None:
            if process.poll() is not None:
                output = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
                raise RuntimeError(f"graphify kb serve exited with status {process.returncode}.\n{_tail(output)}")
            if time.monotonic() > deadline:
                process.terminate()
                raise TimeoutError(f"graphify kb serve did not answer at {url} in {timeout_s} s.")
            time.sleep(0.2)
        self._kb_procs[source.key] = process
        entry = {"url": url, "pid": process.pid, "graph": str(graph), "read_only": read_only}
        _kb_write_json(out / KB_REGISTRY, entry)
        self._kb_checked[source.key] = (url, time.monotonic())
        return url, *running, str(log)

    def _kb_moves(self, path: Path, moved: dict[str, str]) -> tuple[str, ...]:
        """Send the new ids of moved nodes to the knowledge stores. One line for each, never an error.

        The store of the project, `graphify-out/kb/`, gets them through `graphify kb move`. Its server sees
        them too: each write of a store first loads the writes of other processes. The configured server gets
        them as well, unless it serves the project store. A snapshot is a copy, so it gets nothing: the next
        snapshot copies the moved records.
        """
        if not moved:
            return ()
        graph_file = g.graph_path(path).resolve()
        ops = [{"op": "move_node", "from": old, "to": new} for old, new in sorted(moved.items())]
        lines = []
        if _kb_store_saved(graph_file):
            lines.append(self._kb_move_store(path, graph_file, ops))
        try:
            target = self._kb_source(None)
        except (ValueError, OSError) as error:
            return (*lines, f"configured server: not updated: {error}")
        if target.graph is not None:
            return tuple(lines)
        line = self._kb_move_server(target, graph_file, ops)
        return (*lines, line) if line else tuple(lines)

    def _kb_move_store(self, path: Path, graph_file: Path, ops: list[dict]) -> str:
        """Run `graphify kb move` on a store, and describe the outcome in one line."""
        place = _relative(path, str(graph_file.parent / "kb"))
        results: list = []
        for start in range(0, len(ops), KB_MOVE_PAIRS):
            ids = [node_id for op in ops[start : start + KB_MOVE_PAIRS] for node_id in (op["from"], op["to"])]
            argv = graphify_command() + ["kb", "move", *ids, "--graph", str(graph_file), "--json"]
            try:
                done = self._run(argv, cwd=str(path), env=_child_env(), capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                return _kb_outcome(place, results, "graphify kb move did not finish in 300 s")
            except OSError as error:
                return _kb_outcome(place, results, str(error))
            if done.returncode != 0:
                reason = _tail(done.stderr or done.stdout or "", 1) or f"graphify kb move exited with {done.returncode}"
                return _kb_outcome(place, results, reason)
            try:
                results += json.loads(done.stdout or "[]")
            except ValueError:
                return _kb_outcome(place, results, "graphify kb move did not print JSON")
        return _kb_outcome(place, results)

    def _kb_move_server(self, source: _KBSource, graph_file: Path, ops: list[dict]) -> str | None:
        """Send moves to a server, and describe the outcome in one line. None when it serves the project store."""
        results: list = []
        try:
            client = self._kb_client(source, timeout_s=15.0)
            served = client.request("GET", "/api/stats").get("graph")
            if served and Path(str(served)).resolve() == graph_file:
                return None
            for start in range(0, len(ops), KB_BATCH_LIMIT):
                answer = client.request("POST", "/api/batch", {"ops": ops[start : start + KB_BATCH_LIMIT]})
                results += answer.get("results") or []
        except (kb.KBRequestError, ValueError) as error:
            return _kb_outcome(source.name, results, str(error))
        return _kb_outcome(source.name, results)

    def serve(
        self,
        source: str = "local",
        port: int | None = None,
        host: str | None = None,
        read_only: bool | None = None,
        stop: bool = False,
        timeout_s: int = 120,
    ) -> kb.KBServer:
        """Start the server of a knowledge store on this machine, or use the one that runs. Gives its admin UI URL.

        The server is `graphify kb serve`: an HTTP API and an admin UI in the browser. People and agents
        edit the same store there, and each change is undoable. The knowledge calls start this server
        themselves, so call serve to give a person the URL, or to choose the port, the address or read-only
        mode. The server keeps running after the session. `stop=True` stops it, also after a gateway restart.

        Args:
            source: `local` for the store of the workspace root, or the path of another store or snapshot.
            port: TCP port. None keeps the port of the running server, else takes a free port.
            host: Address of this machine to bind. None keeps the address of the running server, else uses
                127.0.0.1. Off loopback, the server needs `$GRAPHIFY_KB_TOKEN` with at least 16 characters,
                and the knowledge calls send that token.
            read_only: Refuse all writes. None keeps the mode of the running server. A snapshot is always read-only.
            stop: Stop the server of the store instead. It writes the pending changes first.
            timeout_s: Stop waiting for a new server after this many seconds.
        """
        if stop:
            return self._kb_stop(source)
        target = self._kb_source(source)
        if target.graph is None:
            raise ValueError(
                f"{target.name} is a server already. serve starts the server of a store on this machine. "
                f"Give source={target.name!r} to the other calls."
            )
        if host is not None and not _local_host(host):
            raise ValueError(
                f"serve starts a server on this machine, and {host} is not one of its addresses. "
                "Give a server elsewhere as source=... to the other calls."
            )
        if target.kind == "snapshot":
            read_only = True
        with self._kb_lock:
            running = self._kb_running(target)
            if running:
                url, health, stats = running
                parts = urlsplit(url)
                asked = []
                if port is not None and port != parts.port:
                    asked.append(f"port {port}")
                if host is not None and host.strip("[]").lower() != (parts.hostname or ""):
                    asked.append(f"host {host}")
                if read_only is not None and bool(read_only) != bool(health.get("read_only")):
                    asked.append("read-only mode" if read_only else "writes")
                if asked:
                    raise RuntimeError(
                        f"{target.name} has a server at {url} already. Stop it with "
                        f"graphify.serve({target.name!r}, stop=True), then start it with {' and '.join(asked)}."
                    )
                pid = (_kb_registry(target.graph) or {}).get("pid")
                return kb.server(health, stats, url, started=False, pid=pid, source=target.name)
            url, health, stats, log = self._kb_start(
                target, port=port, host=host or "127.0.0.1", read_only=bool(read_only), timeout_s=timeout_s
            )
        pid = self._kb_procs[target.key].pid
        return kb.server(health, stats, url, started=True, pid=pid, log=log, source=target.name)

    def _kb_stop(self, source: str) -> kb.KBServer:
        """Stop the server that this extension started for a knowledge store. The server writes pending changes first.

        This works also after a restart of the gateway: the file `kb-serve.json` next to the graph names the server.
        """
        target = self._kb_source(source)
        if target.graph is None:
            raise ValueError(f"{target.name} is a server elsewhere. Stop it where it runs.")
        with self._kb_lock:
            entry = _kb_registry(target.graph) or {}
            process = self._kb_procs.pop(target.key, None)
            self._kb_checked.pop(target.key, None)
            url = str(entry.get("url") or "")
            running = self._kb_serves(url, target.graph) if url else None
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
            pid = int(entry.get("pid") or 0) or (process.pid if process is not None else 0)
            if running is not None and pid and (process is None or process.pid != pid) and _kb_alive(pid):
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 15
                while _kb_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.2)
            (target.graph.parent / KB_REGISTRY).unlink(missing_ok=True)
        if running is None and process is None:
            raise RuntimeError(f"No server of this extension runs for {target.name}.")
        health, stats = running or ({}, {})
        stopped = kb.server(health, stats, url, started=False, pid=pid or None, source=target.name)
        return dataclasses.replace(stopped, stopped=True)

    def _kb_connect(self, base: str, timeout_s: float) -> str:
        """Check a server and its token. Returns the base URL after a redirect on the same host."""
        configured, configured_token, place = self._kb_configured(strict=False)
        # The configured token goes only to the configured server, also after its move to https.
        fallback = configured_token if base == configured else None
        state = self._kb_state()
        client = kb.KBClient(url=base, actor=self._kb_actor, timeout_s=timeout_s)
        try:
            client.request("GET", "/api/health")
        except kb.KBRequestError as error:
            moved = _moved_base(base, error)
            if moved is None:
                raise
            base = client.url = moved
            client.request("GET", "/api/health")
        earlier = state.tokens.get(base)
        client.token = earlier or fallback
        try:
            client.request("GET", "/api/stats")
        except kb.KBRequestError as error:
            if error.status != 401:
                raise
            token_place = "Settings" if place == "Settings" else "GRAPHIFY_KB_TOKEN"
            if earlier:
                refused = "refused the token of this session"
            elif client.token:
                refused = f"refused the token in {token_place}"
            elif configured_token:
                refused = f"needs a token. The token in {token_place} goes only to {configured}"
            else:
                refused = "needs a token"
            client.token = self._kb_ask(base, client)
            if not client.token:
                raise kb.KBRequestError(
                    f"The server at {base} {refused}. Save the server and its token in Settings, or give it in the form.",
                    401,
                ) from None
            client.request("GET", "/api/stats")
        if client.token:
            state.tokens[base] = client.token
        else:
            state.tokens.pop(base, None)
        return base

    def _kb_ask(self, base: str, client: kb.KBClient) -> str | None:
        """Ask the person for the token in a masked form. The form checks each try with the server."""
        if self._kb_ask_token is None:
            return None

        def check(text: str) -> str | None:
            try:
                dataclasses.replace(client, token=text.strip()).request("GET", "/api/stats")
            except kb.KBRequestError as error:
                return "The server refused this token." if error.status == 401 else str(error)
            return None

        token = self._kb_ask_token(base, check)
        return token.strip() if token else None

    def snapshot(
        self, source: str | None = None, name: str | None = None, assets: bool = False, timeout_s: float = 600.0
    ) -> kb.KBSnapshot:
        """Copy a knowledge source into `graphify-out/snapshots/<name>/` of the workspace root.

        The copy holds the graph, the ontology, the knowledge records and the documents. Give its folder as
        `source` to read it without the server. A snapshot is read-only. A new snapshot with the same name
        replaces the old one. The code questions keep reading the local tree-sitter graph.

        Args:
            source: The source to copy, usually a server URL. None copies the configured server, else `local`.
            name: Folder name of the snapshot. None uses the host and port of a server, or the project of a store.
            assets: Also copy the files of the documents, such as images and PDFs.
            timeout_s: Stop waiting for the source after this many seconds.
        """
        target = self._kb_source(source)
        if target.kind == "snapshot":
            raise ValueError(f"{target.name} is a snapshot. Copy the source that it copies.")
        root = Path(self._workspace_root()).resolve()
        folder_name = _kb_slug(name) if name else _kb_snapshot_name(target)
        snapshots = root / g.GRAPH_DIR / KB_SNAPSHOTS
        place = snapshots / folder_name
        query = {"assets": "1" if assets else None}
        answer, client, target = self._kb_call(target, "GET", "/api/export", query=query, timeout_s=timeout_s)
        snapshots.mkdir(parents=True, exist_ok=True)
        _ignore_output(root / g.GRAPH_DIR)
        staging = Path(tempfile.mkdtemp(prefix=f".{folder_name}-", dir=snapshots))
        old = None
        try:
            counts = _kb_write_export(staging, answer)
            meta = {
                "source": target.name,
                "version": int(answer.get("version") or 0),
                "exported_at": str(answer.get("exported_at") or ""),
                "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "assets": bool(assets),
            }
            _kb_write_json(staging / KB_SNAPSHOT_FILE, meta)
            # The server of the old copy serves the new one: it loads the new files at its next read.
            for kept in (KB_REGISTRY, "kb-serve.log"):
                if (place / kept).is_file():
                    shutil.copy2(place / kept, staging / kept)
            if place.exists():
                old = snapshots / f".{folder_name}-old-{time.time_ns()}"
                place.rename(old)
            staging.rename(place)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            if old is not None and not place.exists():
                old.rename(place)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return kb.KBSnapshot(
            source=target.name,
            name=os.path.relpath(place, root),
            path=str(place),
            version=meta["version"],
            nodes=counts["nodes"],
            edges=counts["edges"],
            documents=counts["documents"],
            assets=counts["assets"],
            taken_at=meta["taken_at"],
        )

    def _kb_remember(self, target: _KBSource, seqs: tuple[int, ...]) -> None:
        """Keep the history numbers of an own change, so that undo reverts this change and not the change of another person."""
        if seqs:
            self._kb_state().changes.setdefault(target.key, []).append(tuple(seqs))

    def write(self, ops: list[dict], source: str | None = None) -> kb.KBChange:
        """Apply operations to a knowledge source as one undoable change. One failure applies nothing.

        Each operation is a dict with an `op` key:
          {"op": "add_node", "label": ..., "class": ..., "id": ..., "attrs": {...}}
          {"op": "update_node", "id": ..., "attrs": {...}}   a None value removes the attribute
          {"op": "delete_node", "id": ...}                  also removes its edges
          {"op": "add_edge", "source": ..., "target": ..., "relation": ..., "confidence": ..., "attrs": {...}}
          {"op": "update_edge" | "delete_edge", "id": ...} or with source, target and relation
          {"op": "add_document", "title": ..., "body": markdown, "tags": [...], "nodes": [ids]}
          {"op": "update_document", "id": ..., "title" | "body" | "tags" | "nodes": ...}
          {"op": "delete_document", "id": ...}
          {"op": "move_node", "from": ..., "to": ...}       the records of a code node follow its new id
        A `[[label]]` in a document body links the document to that node. rename, move and move_file
        send `move_node` themselves.

        Args:
            ops: The operations, in order. At most 10000.
            source: `local`, a store path or a server URL. None uses the configured server, else `local`.
        """
        if not isinstance(ops, (list, tuple)) or not ops:
            raise ValueError("ops must be a non-empty list of operation dicts.")
        answer, client, target = self._kb_call(source, "POST", "/api/batch", {"ops": list(ops)})
        self._kb_remember(target, client.changes)
        names = Counter(str(op.get("op")) for op in ops if isinstance(op, dict))
        summary = ", ".join(f"{count} {name}" for name, count in names.items())
        return kb.change_result(f"Applied {summary}", answer, source=target.name, seqs=client.changes)

    def ingest(
        self,
        path: str,
        source: str | None = None,
        mode: str = "text",
        title: str | None = None,
        tags: list[str] | None = None,
        link: bool = True,
        text: str | None = None,
    ) -> kb.KBIngestResult:
        """Add a file, a web page or text to a knowledge source as a document.

        Markdown and text stay as they are. PDF, HTML, images and other files
        become attachments with extracted text when the server can read it.
        `text` mode links the document to the nodes that it names, without a
        model. `llm` mode also asks the graphify model backend for new nodes
        and edges. The file goes to the server, so a remote source works too.

        Args:
            path: A file path relative to the workspace root, an http(s) URL, or the document name when `text` is set.
            source: `local`, a store path or a server URL. None uses the configured server, else `local`.
            mode: `text` or `llm`.
            title: Document title. None uses the file name or the first heading.
            tags: Document tags.
            link: Link the document to the existing nodes that it names.
            text: Document content. Then `path` is only its name.
        """
        if mode not in ("text", "llm"):
            raise ValueError("mode must be 'text' or 'llm'.")
        body: dict = {"mode": mode, "title": title, "tags": list(tags) if tags else None, "link": link}
        if text is not None:
            body.update(name=path, text=text)
        elif path.startswith(("http://", "https://")):
            body.update(name=path.rstrip("/").rsplit("/", 1)[-1] or "page", url=path)
        else:
            file = Path(path).expanduser()
            if not file.is_absolute():
                file = Path(self._workspace_root()) / file
            if not file.is_file():
                raise FileNotFoundError(f"{file} is not a file.")
            body.update(name=file.name, data_base64=base64.b64encode(file.read_bytes()).decode())
        timeout = 900.0 if mode == "llm" else 120.0
        answer, client, target = self._kb_call(source, "POST", "/api/ingest", body, timeout_s=timeout)
        self._kb_remember(target, client.changes)
        return kb.ingest_result(answer, source=target.name)

    def validate(self, source: str | None = None, limit: int = 50, info: bool = False) -> kb.KBReport:
        """Check a knowledge source against its ontology: missing properties, bad edge ends, orphans, cycles.

        Args:
            source: `local`, a store or snapshot path, or a server URL. None uses the configured server, else `local`.
            limit: Maximum count of returned findings. The counts stay complete.
            info: Also return info findings, such as orphan nodes.
        """
        query = {"limit": limit, "info": "1" if info else "0"}
        answer, _, target = self._kb_call(source, "GET", "/api/validate", query=query)
        return kb.report_result(answer, source=target.name)

    def undo(self, source: str | None = None, seq: int | None = None) -> kb.KBChange:
        """Revert the latest change of this session in a knowledge source, or the change with history number `seq`.

        Without `seq`, it reverts the latest write or ingest of this session that is not reverted yet,
        also on a server that other people edit. A change of several steps is reverted newest step first.

        Args:
            source: `local`, a store path or a server URL. None uses the configured server, else `local`.
            seq: History number of a change, such as a number from the history of the admin UI. None reverts
                the latest change of this session.
        """
        target = self._kb_source(source)
        own = self._kb_state().changes.setdefault(target.key, [])
        if seq is None and not own:
            raise ValueError(
                f"This session has no change in {target.name} to revert. "
                "Give seq=N from the history of the admin UI to revert the change of another person."
            )
        steps = own[-1] if seq is None else (int(seq),)
        answer: dict = {}
        for step in sorted(steps, reverse=True):
            answer, _, target = self._kb_call(target, "POST", "/api/undo", {"seq": step})
            own[:] = [kept for group in own if (kept := tuple(s for s in group if s != step))]
        return kb.change_result("Undo", answer, source=target.name)

    def publish(self, source: str | None = None, path: str | None = None, timeout_s: int = 300) -> kb.KBPublishResult:
        """Write a static HTML view of a knowledge source on this machine. It opens without a server.

        A store gets the page in its own output folder. A server first sends a copy of its store, and the
        page goes into `graphify-out/` of the workspace root.

        Args:
            source: `local`, a store or snapshot path, or a server URL. None uses the configured server, else `local`.
            path: An `.html` file name without folders. None writes `kb.html` for a store, or
                `kb-<host>.html` for a server.
            timeout_s: Stop waiting for graphify after this many seconds.
        """
        if path is not None and (Path(path).name != path or not path.endswith(".html") or path == "graph.html"):
            raise ValueError("path must be an .html file name without folders, other than graph.html.")
        target = self._kb_source(source)
        if target.graph is not None:
            if target.kind == "local" and not target.graph.is_file():
                self._update(target.graph.parent.parent)
            page = target.graph.parent / (path or "kb.html")
            running = self._kb_running(target) if target.kind != "snapshot" and target.graph.is_file() else None
            if running:
                # The page reads the files, so the server first writes the changes that it holds in memory.
                url = running[0]
                kb.KBClient(url=url, token=_kb_store_token(url), actor=self._kb_actor, timeout_s=60.0).request(
                    "POST", "/api/flush"
                )
            self._kb_html(target.graph, page, None, timeout_s)
        else:
            answer, _, target = self._kb_call(target, "GET", "/api/export", timeout_s=float(timeout_s))
            out = Path(self._workspace_root()).resolve() / g.GRAPH_DIR
            out.mkdir(parents=True, exist_ok=True)
            _ignore_output(out)
            page = out / (path or f"kb-{_kb_snapshot_name(target)}.html")
            with tempfile.TemporaryDirectory(prefix="vis-graphify-kb-") as temp:
                _kb_write_export(Path(temp), answer)
                self._kb_html(Path(temp) / g.GRAPH_FILE, page, target.name, timeout_s)
        return kb.KBPublishResult(path=str(page), bytes=page.stat().st_size, source=target.name)

    def _kb_html(self, graph: Path, page: Path, title: str | None, timeout_s: int) -> None:
        """Run `graphify kb export` for a store on this machine."""
        argv = graphify_command() + ["kb", "export", str(graph), "--out", str(page)]
        if title:
            argv += ["--name", title]
        try:
            done = self._run(
                argv, cwd=str(graph.parent), env=_child_env(), capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"graphify kb export did not finish in {timeout_s} s.") from error
        if done.returncode != 0 or not page.is_file():
            output = _tail((done.stdout or "") + "\n" + (done.stderr or ""))
            raise RuntimeError(f"graphify kb export exited with status {done.returncode}.\n{output}")
