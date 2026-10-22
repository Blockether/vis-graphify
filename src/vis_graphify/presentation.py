"""Activity presentations for the graphify tools."""

from __future__ import annotations

from typing import Callable

import blockether.vis.extension as vis

from vis_graphify.tools import (
    BuildResult,
    DomainReport,
    Explanation,
    GraphStatus,
    HubsResult,
    ImpactResult,
    Node,
    PathResult,
    ReferencesResult,
    RenameResult,
    SearchResult,
    SignatureResult,
    _is_file,
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
    """Node, Kind and Location columns. Kind stays out when every node has the same kind."""
    kinds = len({node.kind for node in nodes}) > 1
    rows = tuple(
        (node.label,) + ((node.kind,) if kinds else ()) + (_where(node),) + (tuple(values(i)) if values else ())
        for i, node in enumerate(nodes)
    )
    return vis.ActivityTable(
        columns=("Node",) + (("Kind",) if kinds else ()) + ("Location",) + extra,
        rows=rows,
        paths=tuple(node.file for node in nodes),
    )


def build(result: BuildResult) -> vis.ActivityPresentation:
    return vis.ActivityPresentation(
        "Build code graph",
        f"{result.nodes} nodes, {result.edges} edges in {result.seconds:g} s",
        (vis.ActivityText(result.graph_path),),
    )


def status(result: GraphStatus) -> vis.ActivityPresentation:
    if not result.exists:
        return vis.ActivityPresentation(
            "Check code graph",
            "no graph yet",
            (vis.ActivityText(f"The next query builds {result.graph_path}."),),
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


def hubs(result: HubsResult) -> vis.ActivityPresentation:
    rows = result.hubs
    if not rows:
        return vis.ActivityPresentation(
            "List code hubs", "no edges", (vis.ActivityText("The graph has no matching edges."),)
        )
    nodes = tuple(hub.node for hub in rows)
    return vis.ActivityPresentation(
        "List code hubs",
        f"top {len(rows)}, led by {rows[0].node.label}",
        (_node_table(nodes, ("Degree",), lambda i: (str(rows[i].degree),)),),
    )


def affected(result: ImpactResult) -> vis.ActivityPresentation:
    if not result.impacts:
        return vis.ActivityPresentation(
            "Find affected code", "no dependents", (vis.ActivityText(f"Nothing depends on {result.node.label}."),)
        )
    impacts = tuple(impact for impact in result.impacts if not _is_file(impact.node)) or result.impacts
    more = ", list cut by limit" if result.truncated else ""
    content: list = []
    if result.functions:
        public = [name for name in result.functions if name not in result.private]
        text = f"Functions: {', '.join(public)}" if public else "No public functions."
        if result.private:
            text += f"\nPrivate: {', '.join(result.private)}"
        content.append(vis.ActivityText(text))
    content.append(
        _node_table(
            tuple(impact.node for impact in impacts),
            ("Steps", "Through"),
            lambda i: (str(impacts[i].distance), " → ".join(impacts[i].chain) or impacts[i].via),
        )
    )
    if result.others:
        content.append(vis.ActivityText("Not counted, same name:\n" + "\n".join(result.others)))
    calls = sum(1 for impact in impacts if impact.node.label.endswith("()"))
    counted = _plural(calls, "function") if calls else _plural(len(impacts), "dependent")
    return vis.ActivityPresentation(
        "Find affected code",
        f"{result.node.label}: {counted} in {_plural(len(result.files), 'file')}{more}",
        tuple(content),
    )


def references(result: ReferencesResult) -> vis.ActivityPresentation:
    if not result.references:
        return vis.ActivityPresentation(
            "Find code references", "no references", (vis.ActivityText(f"No site names {result.node.label}."),)
        )
    more = ", truncated" if result.truncated else ""
    shown = result.references[:30]
    rows = tuple((f"{ref.file}:{ref.line}:{ref.column}", ref.role, ref.text) for ref in shown)
    content: tuple = (
        vis.ActivityTable(columns=("Location", "Role", "Text"), rows=rows, paths=tuple(ref.file for ref in shown)),
    )
    if len(result.references) > len(shown):
        content += (vis.ActivityText(f"… {len(result.references) - len(shown)} more in the result."),)
    return vis.ActivityPresentation(
        "Find code references",
        f"{_plural(result.total, 'reference')} in {_plural(len(result.files), 'file')}{more}",
        content,
    )


def _check(check: str) -> tuple[str, tuple]:
    """The summary suffix and the content of a project check: `, go vet passed`, and its errors on failure."""
    if not check:
        return "", ()
    head, _, rest = check.partition("\n")
    command, _, outcome = head.rpartition(": ")
    words = command.split(" ")
    stop = next((i for i, word in enumerate(words) if word[:1] in "-./"), len(words))
    tool = " ".join(words[: max(stop, 1)])
    content = (vis.ActivityCode(f"{head}\n{rest}"),) if rest else ()
    return f", {tool} {outcome}", content


def rename(result: RenameResult) -> vis.ActivityPresentation:
    change = f"{result.old} → {result.new}"
    if result.conflicts:
        content = tuple(vis.ActivityText(conflict) for conflict in result.conflicts)
        return vis.ActivityPresentation(
            "Rename symbol", f"{change}: not written, {_plural(len(result.conflicts), 'conflict')}", content
        )
    state = "renamed" if result.applied else "preview"
    checked, failure = _check(result.check)
    content: tuple = failure + ((vis.ActivityDiff(_clip(result.diff, 8000), "diff"),) if result.diff else ())
    if result.mentions:
        kept = "\n".join(result.mentions[:10]) + ("\n…" if len(result.mentions) > 10 else "")
        content += (vis.ActivityText(f"Other code that spells {result.old} and stays:\n{kept}"),)
    return vis.ActivityPresentation(
        "Rename symbol",
        f"{change}: {state}, {_plural(len(result.edits), 'edit')} in {_plural(len(result.files), 'file')}{checked}",
        content,
    )


def add_parameter(result: SignatureResult) -> vis.ActivityPresentation:
    change = f"{result.node.label} + {result.parameter}"
    if result.conflicts:
        content = tuple(vis.ActivityText(conflict) for conflict in result.conflicts)
        return vis.ActivityPresentation(
            "Add parameter", f"{change}: not written, {_plural(len(result.conflicts), 'conflict')}", content
        )
    state = "written" if result.applied else "preview"
    calls = sum(1 for edit in result.edits if edit.kind == "call")
    checked, failure = _check(result.check)
    content: tuple = failure + ((vis.ActivityDiff(_clip(result.diff, 8000), "diff"),) if result.diff else ())
    return vis.ActivityPresentation(
        "Add parameter",
        f"{change}: {state}, {_plural(calls, 'call')} in {_plural(len(result.files), 'file')}{checked}",
        content,
    )


def domain(result: DomainReport) -> vis.ActivityPresentation:
    if not result.plugins:
        content = [vis.ActivityText("No domain plugin applies. Name one in `plugins`.")]
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
