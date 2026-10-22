"""The graphify tools that Vis exposes as `graphify.*`.

This module is plain Python. `extension.py` binds it to Vis.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable

from vis_graphify import graph as g


@dataclass(frozen=True)
class Node:
    """One graph node."""

    id: Annotated[str, "Stable node id; pass it to other calls for an exact match."]
    label: Annotated[str, "Display name, such as `my.ns` or `run-tests()`."]
    file: Annotated[str, "Source file, relative to the project root."]
    line: Annotated[int | None, "1-based line of the definition, if known."]
    kind: Annotated[str, "Language kind, such as `namespace` or `var`, or the file type."]
    community: Annotated[int | None, "Cluster number, if the graph was clustered."]


@dataclass(frozen=True)
class Edge:
    """One edge seen from a node."""

    relation: Annotated[str, "Edge type: calls, references, imports, contains, ..."]
    node: Annotated[Node, "The node at the other end."]
    confidence: Annotated[str, "EXTRACTED for parsed facts, INFERRED for guesses."]
    at: Annotated[str, "Where the edge occurs, as `file:Lline`."]


@dataclass(frozen=True)
class BuildResult:
    """The outcome of one graph build."""

    root: Annotated[str, "Absolute project root."]
    graph_path: Annotated[str, "Absolute path of graph.json."]
    ok: Annotated[bool, "True when graphify exited with status 0."]
    exit_code: Annotated[int, "graphify exit status."]
    nodes: Annotated[int, "Node count after the build."]
    edges: Annotated[int, "Edge count after the build."]
    seconds: Annotated[float, "Wall time of the build."]
    output: Annotated[str, "Last lines of graphify output."]


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


@dataclass(frozen=True)
class SearchResult:
    """Nodes that match a pattern."""

    pattern: Annotated[str, "The case-insensitive regex."]
    total: Annotated[int, "Count of all matches."]
    nodes: Annotated[tuple[Node, ...], "The best matches, labels first, then by degree."]


@dataclass(frozen=True)
class Explanation:
    """One node with its direct neighbours."""

    node: Annotated[Node, "The node that was asked about."]
    outgoing: Annotated[tuple[Edge, ...], "Edges from the node: what it calls, references or contains."]
    incoming: Annotated[tuple[Edge, ...], "Edges to the node: callers, importers, its container."]
    truncated: Annotated[bool, "True when `limit` cut either list."]


@dataclass(frozen=True)
class PathResult:
    """The shortest connection between two nodes."""

    source: Annotated[Node, "Start node."]
    target: Annotated[Node, "End node."]
    found: Annotated[bool, "False when the nodes are not connected."]
    steps: Annotated[tuple[Edge, ...], "Each hop: the relation and the node it reaches."]


@dataclass(frozen=True)
class Hub:
    """One highly connected node."""

    node: Annotated[Node, "The node."]
    degree: Annotated[int, "Count of counted edges."]


@dataclass(frozen=True)
class Impact:
    """One node that depends on a changed node."""

    node: Annotated[Node, "The dependent node."]
    distance: Annotated[int, "Steps from the changed node."]
    via: Annotated[str, "Relation through which it depends, such as `calls`."]


@dataclass(frozen=True)
class ImpactResult:
    """What a change to a node can reach."""

    node: Annotated[Node, "The changed node."]
    depth: Annotated[int, "Maximum distance that was followed."]
    files: Annotated[tuple[str, ...], "Distinct source files of the dependents."]
    impacts: Annotated[tuple[Impact, ...], "Dependents, nearest first."]
    truncated: Annotated[bool, "True when `limit` cut the list."]


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


@dataclass(frozen=True)
class ReferencesResult:
    """Every site that resolves to one symbol."""

    node: Annotated[Node, "The symbol."]
    total: Annotated[int, "Count of all sites."]
    files: Annotated[tuple[str, ...], "Distinct files with a site."]
    references: Annotated[tuple[Reference, ...], "Sites in file and position order."]
    truncated: Annotated[bool, "True when `limit` cut the list."]


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
        tuple[str, ...], "`file:line text` of strings, comments and other symbols that spell the old name and stay."
    ]
    diff: Annotated[str, "Unified diff of the change."]


def _relative(root: Path, file: str) -> str:
    path = Path(file)
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else file


def _raw(graph: g.Graph) -> dict:
    """The graph.json shape that `graphify.refactor` reads, from the cached graph."""
    return {"nodes": list(graph.nodes.values()), "links": [dataclasses.asdict(link) for link in graph.links]}


def _unified_diff(root: Path, contents: dict) -> str:
    chunks = []
    for file, (old, new) in sorted(contents.items()):
        name = _relative(root, file)
        chunks.extend(
            difflib.unified_diff(
                old.decode("utf-8", "replace").splitlines(keepends=True),
                new.decode("utf-8", "replace").splitlines(keepends=True),
                f"a/{name}",
                f"b/{name}",
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


Runner = Callable[..., subprocess.CompletedProcess]


class GraphifyTools:
    """Build and query a graphify knowledge graph of the workspace code."""

    def __init__(self, workspace_root: Callable[[], str] | None = None, runner: Runner | None = None):
        self._workspace_root = workspace_root or os.getcwd
        self._run = runner or subprocess.run

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

        Writes `<root>/graphify-out/graph.json` and `GRAPH_REPORT.md`. A later
        build re-extracts only changed files. Supports Clojure, Python,
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
            BuildResult with the counts and the last lines of graphify output.
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
        return BuildResult(
            root=str(path),
            graph_path=str(g.graph_path(path)),
            ok=True,
            exit_code=done.returncode,
            nodes=nodes,
            edges=edges,
            seconds=seconds,
            output=output,
        )

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
        graph = g.load(self._root(root))
        ids, total = g.search(graph, pattern, limit)
        return SearchResult(pattern, total, tuple(_node(graph, node_id) for node_id in ids))

    def explain(self, node: str, root: str = ".", limit: int = 40) -> Explanation:
        """Show one node with what it calls and what calls or contains it.

        Args:
            node: Node id, label (`run-tests()`), bare name or a qualified name (`ns/name`, `module.name`, `Class.method`, `crate::m::name`).
            root: Project directory, relative to the workspace root.
            limit: Maximum count of edges in each direction.
        """
        graph = g.load(self._root(root))
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
        graph = g.load(self._root(root))
        start, end = g.resolve(graph, source), g.resolve(graph, target)
        steps = g.shortest_path(graph, start, end)
        return PathResult(
            source=_node(graph, start),
            target=_node(graph, end),
            found=bool(steps),
            steps=tuple(_edge(graph, link, node_id) for node_id, link in steps if link is not None),
        )

    def hubs(self, root: str = ".", limit: int = 20, relation: str | None = "calls") -> tuple[Hub, ...]:
        """List the most connected nodes: the code that much else depends on.

        Args:
            root: Project directory, relative to the workspace root.
            limit: Count of nodes to return.
            relation: Count only this relation, such as `calls` or `imports`. None counts all.
        """
        graph = g.load(self._root(root))
        relations = frozenset({relation}) if relation else None
        return tuple(Hub(_node(graph, node_id), degree) for node_id, degree in g.hubs(graph, limit, relations))

    def affected(self, node: str, root: str = ".", depth: int = 2, limit: int = 100) -> ImpactResult:
        """List the code that a change to a node can break: its callers, importers and users.

        Args:
            node: Changed node: id, label, name or a qualified name (`ns/name`, `module.name`, `Class.method`, `crate::m::name`).
            root: Project directory, relative to the workspace root.
            depth: How many dependency steps to follow back.
            limit: Maximum count of returned dependents.
        """
        graph = g.load(self._root(root))
        node_id = g.resolve(graph, node)
        rows = g.affected(graph, node_id, max(1, depth))
        impacts = tuple(Impact(_node(graph, other), distance, link.relation) for other, distance, link in rows[:limit])
        files = tuple(sorted({impact.node.file for impact in impacts if impact.node.file}))
        return ImpactResult(_node(graph, node_id), depth, files, impacts, len(rows) > limit)

    def references(self, node: str, root: str = ".", limit: int = 200) -> ReferencesResult:
        """List every exact site of a symbol: its definition, uses, member calls and imports.

        Works for functions, methods, classes and Clojure vars in every language with a
        tree-sitter grammar. Resolution follows imports, aliases, receiver types and
        locals, so a parameter, a field or another symbol with the same name never counts.
        The files come from the graph; run graphify.build() after you add files.

        Args:
            node: The symbol: id, label or a qualified name (`ns/name`, `module.name`, `Class.method`, `crate::m::name`).
            root: Project directory, relative to the workspace root.
            limit: Maximum count of returned sites.
        """
        from graphify import refactor

        path = self._root(root)
        graph = g.load(path)
        node_id = g.resolve(graph, node)
        sites = refactor.references(_raw(graph), node_id, path)
        found = tuple(Reference(_relative(path, s.file), s.line, s.col, s.role, s.text, s.qualified) for s in sites)
        files = tuple(sorted({ref.file for ref in found}))
        return ReferencesResult(_node(graph, node_id), len(found), files, found[:limit], len(found) > limit)

    def rename(self, node: str, new_name: str, root: str = ".", apply: bool = False) -> RenameResult:
        """Rename a symbol at every site that resolves to it, across files.

        Works for functions, methods, classes and Clojure vars in every language with a
        tree-sitter grammar. Without `apply`, nothing is written: read `conflicts`, `diff`
        and `mentions`, then call again with `apply=True`. A conflict blocks the write: an
        invalid name, a file that already uses the new name, an override or trait method,
        or a common method name on receivers of unknown type. Strings and comments stay;
        `mentions` lists them. A written rename updates the graph.

        Args:
            node: The symbol: id, label or a qualified name (`ns/name`, `module.name`, `Class.method`, `crate::m::name`).
            new_name: The new name, without a namespace or class.
            root: Project directory, relative to the workspace root.
            apply: Write the files when the plan has no conflict.
        """
        from graphify import refactor

        path = self._root(root)
        graph = g.load(path)
        node_id = g.resolve(graph, node)
        plan = refactor.plan_rename(_raw(graph), node_id, new_name, path)
        contents = refactor.rewrite(plan) if plan.edits else {}
        applied = False
        if apply and not plan.conflicts:
            refactor.apply_rename(plan)
            applied = True
            self._update(path)
        return RenameResult(
            node=_node(graph, node_id),
            old=plan.old,
            new=new_name,
            applied=applied,
            files=tuple(_relative(path, f) for f in plan.files),
            edits=tuple(RenameEdit(_relative(path, e.file), e.line, e.col, e.old, e.new) for e in plan.edits),
            conflicts=tuple(c.replace(f"{path}/", "") for c in plan.conflicts),
            mentions=tuple(f"{_relative(path, m.file)}:{m.line} {m.text}" for m in plan.mentions),
            diff=_unified_diff(path, contents),
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
