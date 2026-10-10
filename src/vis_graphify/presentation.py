"""Activity presentations for the graphify tools."""

from __future__ import annotations

from typing import Callable

import blockether.vis.extension as vis

from vis_graphify.tools import (
    BuildResult,
    DomainReport,
    Explanation,
    GraphStatus,
    Hub,
    ImpactResult,
    Node,
    PathResult,
    PluginInfo,
    QueryAnswer,
    SearchResult,
)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _where(node: Node) -> str:
    if not node.file:
        return ""
    return f"{node.file}:{node.line}" if node.line else node.file


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _node_table(nodes: tuple[Node, ...], extra: tuple[str, ...] = (), values=None) -> vis.ActivityTable:
    rows = tuple(
        (node.label, node.kind, _where(node)) + (tuple(values(i)) if values else ()) for i, node in enumerate(nodes)
    )
    return vis.ActivityTable(
        columns=("Node", "Kind", "Location") + extra,
        rows=rows,
        paths=tuple(node.file for node in nodes),
    )


def build(result: BuildResult) -> vis.ActivityPresentation:
    return vis.ActivityPresentation(
        "Build code graph",
        f"{result.nodes} nodes, {result.edges} edges in {result.seconds:g} s",
        (vis.ActivityText(result.graph_path), vis.ActivityCode(_clip(result.output, 4000)))
        if result.output
        else (vis.ActivityText(result.graph_path),),
    )


def status(result: GraphStatus) -> vis.ActivityPresentation:
    if not result.exists:
        return vis.ActivityPresentation(
            "Check code graph",
            "no graph yet",
            (vis.ActivityText(f"Run graphify.build() to create {result.graph_path}."),),
        )
    rows = tuple((relation, str(count)) for relation, count in result.relations.items())
    return vis.ActivityPresentation(
        "Check code graph",
        f"{result.nodes} nodes, {result.edges} edges, {_plural(result.files, 'file')}",
        (
            vis.ActivityText(f"{result.graph_path} (built {result.built_at})"),
            vis.ActivityTable(("Relation", "Edges"), rows),
        ),
    )


def search(result: SearchResult) -> vis.ActivityPresentation:
    if not result.nodes:
        return vis.ActivityPresentation(
            "Search code graph", "no matches", (vis.ActivityText(f"No node matches {result.pattern!r}."),)
        )
    shown = f"{len(result.nodes)} of {result.total}" if result.total > len(result.nodes) else str(result.total)
    return vis.ActivityPresentation(
        "Search code graph", f"{shown} matches for {result.pattern!r}", (_node_table(result.nodes),)
    )


def _edge_rows(edges) -> tuple[tuple[str, ...], ...]:
    return tuple((edge.relation, edge.node.label, _where(edge.node)) for edge in edges)


def explain(result: Explanation) -> vis.ActivityPresentation:
    content: list = [
        vis.ActivityText(f"{result.node.label} ({result.node.kind}) at {_where(result.node) or 'unknown location'}")
    ]
    if result.outgoing:
        content += [
            vis.ActivityHeading("Outgoing"),
            vis.ActivityTable(("Relation", "Node", "Location"), _edge_rows(result.outgoing)),
        ]
    if result.incoming:
        content += [
            vis.ActivityHeading("Incoming"),
            vis.ActivityTable(("Relation", "Node", "Location"), _edge_rows(result.incoming)),
        ]
    more = ", truncated" if result.truncated else ""
    return vis.ActivityPresentation(
        "Explain code node",
        f"{result.node.label}: {len(result.outgoing)} outgoing, {len(result.incoming)} incoming{more}",
        tuple(content),
    )


def shortest_path(result: PathResult) -> vis.ActivityPresentation:
    ends = f"{result.source.label} → {result.target.label}"
    if not result.found:
        return vis.ActivityPresentation(
            "Find code path", "not connected", (vis.ActivityText(f"No path connects {ends}."),)
        )
    rows = ((("start"), result.source.label, _where(result.source)),) + _edge_rows(result.steps)
    return vis.ActivityPresentation(
        "Find code path",
        f"{_plural(len(result.steps), 'hop')}: {ends}",
        (vis.ActivityTable(("Relation", "Node", "Location"), rows),),
    )


def hubs(result: tuple[Hub, ...]) -> vis.ActivityPresentation:
    if not result:
        return vis.ActivityPresentation(
            "List code hubs", "no edges", (vis.ActivityText("The graph has no matching edges."),)
        )
    nodes = tuple(hub.node for hub in result)
    return vis.ActivityPresentation(
        "List code hubs",
        f"top {len(result)}, led by {result[0].node.label}",
        (_node_table(nodes, ("Degree",), lambda i: (str(result[i].degree),)),),
    )


def affected(result: ImpactResult) -> vis.ActivityPresentation:
    if not result.impacts:
        return vis.ActivityPresentation(
            "Find affected code", "no dependents", (vis.ActivityText(f"Nothing depends on {result.node.label}."),)
        )
    nodes = tuple(impact.node for impact in result.impacts)
    more = ", truncated" if result.truncated else ""
    return vis.ActivityPresentation(
        "Find affected code",
        f"{_plural(len(result.impacts), 'dependent')} in {_plural(len(result.files), 'file')}{more}",
        (
            _node_table(
                nodes,
                ("Distance", "Via"),
                lambda i: (str(result.impacts[i].distance), result.impacts[i].via),
            ),
        ),
    )


def query(result: QueryAnswer) -> vis.ActivityPresentation:
    lines = result.text.splitlines()
    if not lines:
        return vis.ActivityPresentation(
            "Query code graph", "no answer", (vis.ActivityText("graphify found no matching nodes."),)
        )
    nodes = sum(1 for line in lines if line.startswith("NODE"))
    edges = sum(1 for line in lines if line.startswith("EDGE"))
    more = ", truncated" if "TRUNCATED" in result.text else ""
    return vis.ActivityPresentation(
        "Query code graph",
        f"{_plural(nodes, 'node')}, {_plural(edges, 'edge')}{more}",
        (vis.ActivityCode(_clip(result.text, 6000)),),
    )


def domain(result: DomainReport) -> vis.ActivityPresentation:
    if not result.plugins:
        content = [
            vis.ActivityText("No domain plugin applies. Name one in `plugins`, or list them with domain_plugins().")
        ]
        content += [vis.ActivityCode(error) for error in result.plugin_errors]
        return vis.ActivityPresentation("Map domain flows", "no plugin applies", tuple(content))
    kinds = ", ".join(_plural(count, kind) for kind, count in result.counts.items())
    summary = (
        f"{', '.join(result.plugins)}: {_plural(len(result.services), 'service')}, {kinds}, "
        f"{_plural(result.errors, 'error')}, {_plural(result.warnings, 'warning')}"
    )
    content: list = [vis.ActivityText(result.markdown_path)]
    content += [vis.ActivityCode(f"plugin error: {error}") for error in result.plugin_errors]
    problems = [f for f in result.findings if f.severity in ("error", "warning")]
    if problems:
        rows = tuple(
            (f.severity, f.code, _clip(f.message, 160), f"{f.file}:{f.line}" if f.file and f.line else f.file)
            for f in problems[:50]
        )
        content += [
            vis.ActivityHeading("Findings"),
            vis.ActivityTable(
                ("Severity", "Code", "Message", "Location"), rows, paths=tuple(f.file for f in problems[:50])
            ),
        ]
    return vis.ActivityPresentation("Map domain flows", summary, tuple(content))


def domain_plugins(result: tuple[PluginInfo, ...]) -> vis.ActivityPresentation:
    if not result:
        return vis.ActivityPresentation(
            "List domain plugins", "no plugins", (vis.ActivityText("graphify found no domain plugin."),)
        )
    failed = sum(1 for plugin in result if plugin.name == "<error>")
    more = f", {_plural(failed, 'load error')}" if failed else ""
    rows = tuple((plugin.name, _clip(plugin.description, 160), plugin.module) for plugin in result)
    return vis.ActivityPresentation(
        "List domain plugins",
        f"{_plural(len(result) - failed, 'plugin')}{more}",
        (vis.ActivityTable(("Plugin", "Description", "Module"), rows),),
    )


def _arguments(names: tuple[str, ...]) -> Callable[[tuple, dict], tuple]:
    def describe(args: tuple, kwargs: dict) -> tuple:
        # A bound method can arrive with its instance first; skip it.
        if args and not isinstance(args[0], (str, int, float, bool)):
            args = args[1:]
        values = dict(zip(names, args))
        values.update({key: value for key, value in kwargs.items() if key in names})
        shown = ", ".join(f"{key}={values[key]!r}" for key in names if key in values)
        return (vis.ActivityText(shown),) if shown else ()

    return describe


def renderer(label: str, build_presentation, argument_names: tuple[str, ...] = ()):
    """A `vis.Activity` render callback: the result on success, the error on failure."""
    describe = _arguments(argument_names)

    def render(*, phase, args=(), kwargs=None, result=None, error=None, **_):
        if phase == "success" and result is not None:
            return build_presentation(result)
        if phase == "failure":
            detail = _clip(str(error), 1500) if error else "no detail"
            content = describe(tuple(args or ()), dict(kwargs or {}))
            return vis.ActivityPresentation(label, "failed", content + (vis.ActivityCode(detail),))
        if phase == "start":
            content = describe(tuple(args or ()), dict(kwargs or {}))
            if content:
                return vis.ActivityPresentation(label, "running", content)
        return None

    return render
