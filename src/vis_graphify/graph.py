"""Read a graphify `graph.json` and answer structural questions about it.

This module is plain Python. It knows nothing about Vis.
"""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

GRAPH_DIR = "graphify-out"
GRAPH_FILE = "graph.json"


class GraphMissing(FileNotFoundError):
    """No graph.json exists yet for a project."""


class NodeNotFound(LookupError):
    """No node matches a reference."""


class AmbiguousNode(ValueError):
    """More than one node matches a reference."""

    def __init__(self, reference: str, candidates: list[str], places: dict[str, str] | None = None):
        self.reference = reference
        self.candidates = candidates
        places = places or {}
        shown = "\n".join(f"  {node_id}  {places.get(node_id, '')}".rstrip() for node_id in candidates[:10])
        more = f"\n  … {len(candidates) - 10} more" if len(candidates) > 10 else ""
        super().__init__(
            f"{reference!r} matches {len(candidates)} nodes:\n{shown}{more}\n"
            "Pass one node id, a qualified name such as `pkg.Name` or `Class.method`, or `file::name`."
        )


@dataclass(frozen=True)
class Link:
    """One edge as graphify stores it: `source` to `target`."""

    source: str
    target: str
    relation: str
    confidence: str
    source_file: str
    source_location: str


@dataclass
class Graph:
    """An indexed graph.json."""

    path: Path
    nodes: dict[str, dict]
    links: list[Link]
    outgoing: dict[str, list[Link]] = field(default_factory=dict)
    incoming: dict[str, list[Link]] = field(default_factory=dict)

    def __post_init__(self):
        for link in self.links:
            self.outgoing.setdefault(link.source, []).append(link)
            self.incoming.setdefault(link.target, []).append(link)

    def degree(self, node_id: str) -> int:
        return len(self.outgoing.get(node_id, ())) + len(self.incoming.get(node_id, ()))


def graph_path(root: str | Path) -> Path:
    """The graph.json path of a project root."""
    return Path(root) / GRAPH_DIR / GRAPH_FILE


def stale(graph: Graph, root: str | Path) -> bool:
    """Whether a source file of the graph, or a folder that holds one, changed after the build.

    A new or deleted file changes its folder, so it counts too. A path that does not
    exist is skipped.
    """
    built = graph.path.stat().st_mtime_ns
    root = Path(root)
    files = {str(node.get("source_file")) for node in graph.nodes.values() if node.get("source_file")}
    places = {root / file for file in files} | {(root / file).parent for file in files}
    for place in places:
        try:
            if place.stat().st_mtime_ns > built:
                return True
        except FileNotFoundError:
            continue
    return False


def parse(path: Path, data: dict) -> Graph:
    """Index the decoded content of a graph.json."""
    nodes = {node["id"]: node for node in data.get("nodes", ()) if "id" in node}
    links = [
        Link(
            source=str(item["source"]),
            target=str(item["target"]),
            relation=str(item.get("relation", "")),
            confidence=str(item.get("confidence", "")),
            source_file=str(item.get("source_file") or ""),
            source_location=str(item.get("source_location") or ""),
        )
        for item in data.get("links", data.get("edges", ()))
        if "source" in item and "target" in item
    ]
    return Graph(path=path, nodes=nodes, links=links)


_CACHE: dict[Path, tuple[tuple[int, int], Graph]] = {}


def load(root: str | Path) -> Graph:
    """Load the graph of a project root. Reuse it while the file does not change.

    Raises:
        GraphMissing: The project has no graph.json yet.
    """
    path = graph_path(root).resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        raise GraphMissing(f"No graph at {path}. Run graphify.build() first.") from None
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    graph = parse(path, json.loads(path.read_text(encoding="utf-8")))
    _CACHE[path] = (stamp, graph)
    return graph


def _bare(label: str) -> str:
    return label[:-2] if label.endswith("()") else label


def resolve(graph: Graph, reference: str) -> str:
    """The id of the one node that `reference` names.

    The first rule with matches wins: exact id, exact label, label without the
    trailing `()`, the `module/name` of the node metadata (this also finds
    Clojure protocol methods, labelled `.-method()`), a qualified name (`ns/name`,
    `module.name`, `Class.method` or `crate::module::name`), then a
    case-insensitive substring of the label. A node without a source file, such as
    an external import, counts only when no defined node matches. A `file::name`
    reference, such as `src/util.rs::checksum`, also accepts `:` or `#` and applies
    the rules to the nodes of files whose path ends with `file`.

    Raises:
        NodeNotFound: No node matches.
        AmbiguousNode: More than one node matches the first rule with matches.
    """
    reference = reference.strip()
    if reference in graph.nodes:
        return reference
    scope = _IN_FILE.match(reference)
    where = scope["file"].removeprefix("./") if scope else None
    if scope:
        reference = scope["name"]

    def located(node: dict) -> bool:
        file = str(node.get("source_file") or "")
        return where is None or file == where or file.endswith("/" + where)

    bare = _bare(reference)
    lowered = bare.lower()
    rules = (
        lambda node: node.get("label") == reference,
        lambda node: _bare(str(node.get("label", ""))) == bare,
        lambda node: _metadata_name(node) == bare,
        lambda node: _qualifies(graph, node, bare),
        lambda node: lowered in str(node.get("label", "")).lower(),
    )
    for rule in rules:
        found = sorted(node_id for node_id, node in graph.nodes.items() if located(node) and rule(node))
        # A node without a source file, such as an external import, loses to a defined node.
        found = [node_id for node_id in found if graph.nodes[node_id].get("source_file")] or found
        if len(found) == 1:
            return found[0]
        if found:
            raise AmbiguousNode(reference, found, {node_id: _place(graph.nodes[node_id]) for node_id in found})
    raise NodeNotFound(f"No node matches {reference!r}. Try graphify.search().")


def _place(node: dict) -> str:
    """`label at file:line` of a raw node, for error messages."""
    where = node.get("source_file") or ""
    line = node.get("source_location") or ""
    return f"{node.get('label', '')} at {where}:{line.lstrip('Ll')}" if where else node.get("label", "")


def _metadata_name(node: dict) -> str | None:
    """The `module/name` of a node whose metadata has both, such as a Clojure var."""
    meta = node.get("metadata") or {}
    if meta.get("module") and meta.get("name"):
        return f"{meta['module']}/{meta['name']}"
    return None


_QUALIFIED = re.compile(r"^(.+)(?:::|[/.])([^/.:]+)$")
_IN_FILE = re.compile(r"^(?P<file>[^\s:#]+\.[A-Za-z0-9]+)(?:::|:|#)(?P<name>\S+)$")


def _qualifies(graph: Graph, node: dict, qualified: str) -> bool:
    """Whether a qualified reference such as `my.ns/fn` or `Ledger.post` names this node.

    The module of the node metadata decides first, then the class that owns a method,
    then the source file path.
    """
    match = _QUALIFIED.match(qualified)
    if not match or _bare(str(node.get("label", ""))).lstrip(".") != match.group(2):
        return False
    prefix = re.sub(r"^(?:crate|self|super)::", "", match.group(1))
    module = str((node.get("metadata") or {}).get("module") or "")
    if module:
        return module == prefix
    owners = {
        _bare(str(graph.nodes.get(link.source, {}).get("label", "")))
        for link in graph.incoming.get(node.get("id", ""), ())
        if link.relation == "method"
    }
    if owners:
        return re.split(r"::|[/.]", prefix)[-1] in owners
    hint = prefix.replace("::", "/").replace("-", "_").replace(".", "/").lower()
    return hint in str(node.get("source_file", "")).replace("-", "_").lower()


def search(graph: Graph, pattern: str, limit: int) -> tuple[list[str], int]:
    """Ids of nodes whose label, id or source file match a case-insensitive regex.

    A pattern that is not a valid regex, such as the Clojure name `*out*`, matches as plain text.
    Returns the first `limit` ids, best first, and the total match count.
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)
    found = [
        node_id
        for node_id, node in graph.nodes.items()
        if regex.search(str(node.get("label", "")))
        or regex.search(node_id)
        or regex.search(str(node.get("source_file", "")))
    ]
    found.sort(
        key=lambda node_id: (
            not regex.search(str(graph.nodes[node_id].get("label", ""))),
            -graph.degree(node_id),
            node_id,
        )
    )
    return found[:limit], len(found)


def shortest_path(graph: Graph, source: str, target: str) -> list[tuple[str, Link | None]]:
    """The shortest chain of nodes between two ids, edges in either direction.

    Each step is a node id and the link that reached it (None for the start).
    The result is empty when no path exists.
    """
    previous: dict[str, tuple[str, Link] | None] = {source: None}
    queue = deque([source])
    while queue:
        current = queue.popleft()
        if current == target:
            break
        for link in graph.outgoing.get(current, []) + graph.incoming.get(current, []):
            other = link.target if link.source == current else link.source
            if other not in previous:
                previous[other] = (current, link)
                queue.append(other)
    if target not in previous:
        return []
    steps: list[tuple[str, Link | None]] = []
    node: str | None = target
    while node is not None:
        before = previous[node]
        steps.append((node, before[1] if before else None))
        node = before[0] if before else None
    return list(reversed(steps))


# Relations through which a change reaches the code that depends on it.
DEPENDENT_RELATIONS = frozenset({"calls", "references", "imports", "implements", "method"})


def affected(graph: Graph, node_id: str, depth: int) -> list[tuple[str, int, Link]]:
    """Nodes that depend on `node_id`, up to `depth` steps back.

    Follows incoming dependency edges (`calls`, `references`, `imports`, ...).
    Each row is a node id, its distance and the link it depends through.
    """
    seen = {node_id}
    rows: list[tuple[str, int, Link]] = []
    frontier = [node_id]
    for distance in range(1, depth + 1):
        following = []
        for current in frontier:
            for link in graph.incoming.get(current, ()):
                if link.relation not in DEPENDENT_RELATIONS or link.source in seen:
                    continue
                seen.add(link.source)
                rows.append((link.source, distance, link))
                following.append(link.source)
        frontier = following
        if not frontier:
            break
    return rows


def hubs(graph: Graph, limit: int, relations: frozenset[str] | None = None) -> list[tuple[str, int]]:
    """The most connected nodes, by count of edges with one of `relations`."""
    counts: dict[str, int] = {}
    for link in graph.links:
        if relations is not None and link.relation not in relations:
            continue
        counts[link.source] = counts.get(link.source, 0) + 1
        counts[link.target] = counts.get(link.target, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [(node_id, count) for node_id, count in ranked if node_id in graph.nodes][:limit]


def moved_ids(
    before: Graph, after: Graph, files: dict[str, str], names: dict[str, str], scope: set[str]
) -> dict[str, str]:
    """Pair the node ids that a rename or a move replaced: old id to new id.

    `files` maps each moved file, or the file of a moved definition, to its new file.
    `names` maps old names to new names, and `scope` names the files, before the change,
    that can hold a node with a new id. Only an id that left the graph pairs, and only with
    an id that came in, in the same file type. A node of a moved file keeps the part of its
    id after the file prefix. Else it pairs with the one new node in its new file with its
    label, or with its label after the rename. A new id that two old ids claim pairs with
    neither.
    """
    gone = {
        node_id: node
        for node_id, node in before.nodes.items()
        if node_id not in after.nodes and node.get("source_file") in scope
    }
    came = {node_id: node for node_id, node in after.nodes.items() if node_id not in before.nodes}
    claims: dict[str, str] = {}
    for old_file, new_file in files.items():
        old_prefix = _id_prefix([i for i, node in before.nodes.items() if node.get("source_file") == old_file])
        new_prefix = _id_prefix([i for i, node in after.nodes.items() if node.get("source_file") == new_file])
        if not old_prefix or not new_prefix:
            continue
        for node_id, node in gone.items():
            if node.get("source_file") != old_file or not node_id.startswith(old_prefix):
                continue
            candidate = new_prefix + node_id[len(old_prefix) :]
            fresh = came.get(candidate)
            if fresh and fresh.get("source_file") == new_file and fresh.get("file_type") == node.get("file_type"):
                claims[node_id] = candidate
    index: dict[tuple, list[str]] = {}
    for node_id, node in came.items():
        index.setdefault((node.get("source_file"), node.get("label"), node.get("file_type")), []).append(node_id)
    for node_id, node in gone.items():
        if node_id in claims:
            continue
        file = files.get(node.get("source_file"), node.get("source_file"))
        label = str(node.get("label") or "")
        found = {
            candidate
            for name in {label, _renamed(label, names)}
            for candidate in index.get((file, name, node.get("file_type")), ())
        }
        if len(found) == 1:
            claims[node_id] = found.pop()
    taken = Counter(claims.values())
    return {old: new for old, new in sorted(claims.items()) if taken[new] == 1}


def _id_prefix(ids: list[str]) -> str:
    """The start that all `ids` share, cut after a whole `_` part: the id of their file."""
    if not ids:
        return ""
    low, high = min(ids), max(ids)
    size = 0
    while size < len(low) and low[size] == high[size]:
        size += 1
    prefix = low[:size]
    if all(node_id == prefix or node_id.startswith(prefix + "_") for node_id in ids):
        return prefix
    return prefix[: prefix.rfind("_")] if "_" in prefix else ""


def _renamed(label: str, names: dict[str, str]) -> str:
    """`label` with a new name: `old` gives `new`, `old()` gives `new()` and `.old()` gives `.new()`."""
    if label in names:
        return names[label]
    head = "." if label.startswith(".") else ""
    tail = "()" if label.endswith("()") else ""
    core = label[len(head) : len(label) - len(tail)]
    return head + names[core] + tail if core in names else label
