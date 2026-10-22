"""Read a graphify `graph.json` and answer structural questions about it.

This module is plain Python. It knows nothing about Vis.
"""

from __future__ import annotations

import json
import re
from collections import deque
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

    def __init__(self, reference: str, candidates: list[str]):
        self.reference = reference
        self.candidates = candidates
        shown = ", ".join(candidates[:10])
        more = f" and {len(candidates) - 10} more" if len(candidates) > 10 else ""
        super().__init__(
            f"{reference!r} matches {len(candidates)} nodes: {shown}{more}. Pass a node id or a more exact label."
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
        raise GraphMissing(f"No graph at {path}. Build it first with graphify.build().") from None
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
    Clojure protocol methods, labelled `.-method()`), the last segment of a
    qualified label (`ns/name` or `module.name`), then a case-insensitive
    substring of the label.

    Raises:
        NodeNotFound: No node matches.
        AmbiguousNode: More than one node matches the first rule with matches.
    """
    reference = reference.strip()
    if reference in graph.nodes:
        return reference
    bare = _bare(reference)
    lowered = bare.lower()
    rules = (
        lambda node: node.get("label") == reference,
        lambda node: _bare(str(node.get("label", ""))) == bare,
        lambda node: _metadata_name(node) == bare,
        lambda node: re.split(r"[/.]", bare)[-1] == _bare(str(node.get("label", ""))) and _qualifies(node, bare),
        lambda node: lowered in str(node.get("label", "")).lower(),
    )
    for rule in rules:
        found = sorted(node_id for node_id, node in graph.nodes.items() if rule(node))
        if len(found) == 1:
            return found[0]
        if found:
            raise AmbiguousNode(reference, found)
    raise NodeNotFound(f"No node matches {reference!r}. Try graphify.search().")


def _metadata_name(node: dict) -> str | None:
    """The `module/name` of a node whose metadata has both, such as a Clojure var."""
    meta = node.get("metadata") or {}
    if meta.get("module") and meta.get("name"):
        return f"{meta['module']}/{meta['name']}"
    return None


def _qualifies(node: dict, qualified: str) -> bool:
    """Whether a qualified reference such as `my.ns/fn` names this node."""
    name = re.split(r"[/.]", qualified)[-1]
    prefix = qualified[: -len(name) - 1]
    if not prefix:
        return False
    module = str((node.get("metadata") or {}).get("module") or "")
    if module:
        return module == prefix
    hint = prefix.replace("-", "_").replace(".", "/").lower()
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
