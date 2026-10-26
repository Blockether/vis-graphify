"""Activity presentations for the graphify tools."""

from __future__ import annotations

from typing import Callable

import blockether.vis.extension as vis

from vis_graphify.kb import (
    KBChange,
    KBIngestResult,
    KBPublishResult,
    KBReport,
    KBServer,
    KBSnapshot,
)
from vis_graphify.tools import (
    BuildResult,
    CrapResult,
    DomainReport,
    Explanation,
    GraphStatus,
    HubsResult,
    ImpactResult,
    MoveResult,
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


def _hit_table(hits) -> vis.ActivityTable:
    rows = tuple((hit.label, hit.kind, hit.cls, hit.source_file) for hit in hits)
    return vis.ActivityTable(("Label", "Kind", "Class", "Source"), rows, paths=tuple(hit.source_file for hit in hits))


def search(result: SearchResult) -> vis.ActivityPresentation:
    note = (vis.ActivityText(result.note),) if result.note else ()
    if not result.nodes and not result.knowledge:
        return vis.ActivityPresentation(
            "Search graph", "no matches", (vis.ActivityText(f"Nothing matches {result.pattern!r}."),) + note
        )
    shown = f"{len(result.nodes)} of {result.total}" if result.total > len(result.nodes) else str(result.total)
    word = "match" if result.total + len(result.knowledge) == 1 else "matches"
    if not result.knowledge:
        return vis.ActivityPresentation(
            "Search graph", f"{shown} {word} for {result.pattern!r}", (_node_table(result.nodes),) + note
        )
    parts = ([f"{shown} code"] if result.nodes else []) + [f"{len(result.knowledge)} knowledge"]
    content: list = [vis.ActivityHeading("Code"), _node_table(result.nodes)] if result.nodes else []
    content += [vis.ActivityHeading(f"Knowledge in {result.source}"), _hit_table(result.knowledge)]
    return vis.ActivityPresentation(
        "Search graph", f"{' and '.join(parts)} {word} for {result.pattern!r}", tuple(content) + note
    )


def _edge_rows(edges) -> tuple[tuple[str, ...], ...]:
    # A knowledge node has no file: the knowledge is its location.
    return tuple(
        (edge.relation, edge.node.label, _where(edge.node) or ("knowledge" if edge.knowledge else "")) for edge in edges
    )


def explain(result: Explanation) -> vis.ActivityPresentation:
    node = result.node
    where = _where(node)
    if result.attributes:
        # A knowledge node or document: its attributes and text come from the knowledge source.
        head = f"{node.label} ({node.kind}) [{node.id}] in {result.source}" + (f", {where}" if where else "")
    else:
        head = f"{node.label} ({node.kind}) at {where or 'unknown location'}"
    content: list = [vis.ActivityText(head)]
    rows = tuple(
        (key, _clip(str(value), 300)) for key, value in result.attributes.items() if key not in ("id", "label", "body")
    )
    if rows:
        content.append(vis.ActivityTable(("Attribute", "Value"), rows))
    if result.attributes.get("body"):
        content += [vis.ActivityHeading("Text"), vis.ActivityText(_clip(str(result.attributes["body"]), 2000))]
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
    if result.note:
        content.append(vis.ActivityText(result.note))
    known = sum(edge.knowledge for edge in result.outgoing + result.incoming)
    extra = f", {_plural(known, 'knowledge edge')}" if known and not result.attributes else ""
    more = ", truncated" if result.truncated else ""
    return vis.ActivityPresentation(
        "Explain node",
        f"{node.label}: {len(result.outgoing)} outgoing, {len(result.incoming)} incoming{extra}{more}",
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


def crap(result: CrapResult) -> vis.ActivityPresentation:
    if not result.total:
        return vis.ActivityPresentation(
            "Score code risk", "no functions", (vis.ActivityText("No Clojure or Python function matched."),)
        )
    shown = result.entries[:30]
    rows = tuple(
        (
            entry.name,
            f"{entry.file}:{entry.line}",
            str(entry.complexity),
            "–" if entry.coverage is None else f"{entry.coverage:.0f}%",
            "–" if entry.crap is None else f"{entry.crap:.1f}",
        )
        for entry in shown
    )
    content: list = [
        vis.ActivityTable(
            columns=("Function", "Location", "CC", "Coverage", "CRAP"),
            rows=rows,
            paths=tuple(entry.file for entry in shown),
        )
    ]
    if result.total > len(shown):
        content.append(vis.ActivityText(f"… {result.total - len(shown)} more functions."))
    if not result.coverage_reports:
        content.append(vis.ActivityText("No coverage report: the scores are complexity only."))
    if result.stale_reports:
        content.append(vis.ActivityText("Older than the code: " + ", ".join(result.stale_reports)))
    if result.parse_errors:
        content.append(vis.ActivityText("Not parsed:\n" + "\n".join(result.parse_errors[:10])))
    measured = f", {result.measured} with coverage" if result.coverage_reports else ", no coverage"
    return vis.ActivityPresentation(
        "Score code risk",
        f"{result.over} of {_plural(result.total, 'function')} over {result.threshold:g}{measured}",
        tuple(content),
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
    if result.kb:
        content += (vis.ActivityText("Knowledge base:\n" + "\n".join(result.kb)),)
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


def _move(label: str, result: MoveResult) -> vis.ActivityPresentation:
    change = f"{result.subject}: {result.source} → {result.target}"
    if result.conflicts:
        content = tuple(vis.ActivityText(conflict) for conflict in result.conflicts)
        return vis.ActivityPresentation(
            label, f"{change}: not written, {_plural(len(result.conflicts), 'conflict')}", content
        )
    state = "moved" if result.applied else "preview"
    checked, failure = _check(result.check)
    content: tuple = failure
    if result.renames:
        content += (vis.ActivityText("Renamed: " + ", ".join(result.renames)),)
    if result.notes:
        content += (vis.ActivityText("\n".join(result.notes)),)
    if result.diff:
        content += (vis.ActivityDiff(_clip(result.diff, 8000), "diff"),)
    if result.mentions:
        kept = "\n".join(result.mentions[:10]) + ("\n…" if len(result.mentions) > 10 else "")
        content += (vis.ActivityText(f"Other code that names the old place and stays:\n{kept}"),)
    if result.kb:
        content += (vis.ActivityText("Knowledge base:\n" + "\n".join(result.kb)),)
    return vis.ActivityPresentation(label, f"{change}: {state}, {_plural(len(result.files), 'file')}{checked}", content)


def move(result: MoveResult) -> vis.ActivityPresentation:
    return _move("Move definition", result)


def move_file(result: MoveResult) -> vis.ActivityPresentation:
    return _move("Move file", result)


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


def _kb_where(source: str, url: str) -> str:
    return f"{source} at {url}" if source and source != url else url or source


def serve(result: KBServer) -> vis.ActivityPresentation:
    where = _kb_where(result.source, result.url)
    if result.stopped:
        return vis.ActivityPresentation("Stop knowledge base", f"stopped {where}", ())
    label = "Start knowledge base" if result.started else "Use knowledge base"
    mode = ", read-only" if result.read_only else ""
    summary = f"{where}{mode}: {result.nodes} nodes, {result.edges} edges, {_plural(result.documents, 'document')}"
    content = [vis.ActivityText(result.graph_path)] if result.graph_path else []
    if result.log_path:
        content.append(vis.ActivityText(f"Log: {result.log_path}"))
    return vis.ActivityPresentation(label, summary, tuple(content))


def snapshot(result: KBSnapshot) -> vis.ActivityPresentation:
    files = f", {_plural(result.assets, 'document file')}" if result.assets else ""
    summary = (
        f"{result.source} to {result.name}: {result.nodes} nodes, {result.edges} edges, "
        f"{_plural(result.documents, 'document')}{files}"
    )
    return vis.ActivityPresentation("Snapshot knowledge base", summary, (vis.ActivityText(result.path),))


def change(label: str):
    def build(result: KBChange) -> vis.ActivityPresentation:
        rows = tuple(
            (str(r.get("id") or r.get("value") or ""), str(r.get("label") or r.get("title") or r.get("relation") or ""))
            for r in result.results[:50]
        )
        content = (vis.ActivityTable(("Id", "Label"), rows),) if rows else ()
        where = f" in {result.source}" if result.source else ""
        return vis.ActivityPresentation(label, f"{result.summary}{where}, version {result.version}", content)

    return build


def ingest(result: KBIngestResult) -> vis.ActivityPresentation:
    summary = (
        f"{result.title} ({result.mode}): {_plural(len(result.linked), 'linked node')}, "
        f"{_plural(len(result.nodes_added), 'new node')}, {_plural(result.edges_added, 'new edge')}"
    )
    content: list = [vis.ActivityText(result.document_id)]
    if result.linked:
        content.append(vis.ActivityText("Linked: " + _clip(", ".join(result.linked), 600)))
    content += [vis.ActivityText(note) for note in result.notes]
    return vis.ActivityPresentation("Ingest document", summary, tuple(content))


def validate(result: KBReport) -> vis.ActivityPresentation:
    summary = (
        f"{_plural(result.errors, 'error')}, {_plural(result.warnings, 'warning')}, {_plural(result.notes, 'note')}"
    )
    if not result.findings:
        return vis.ActivityPresentation("Validate knowledge base", summary, (vis.ActivityText("No findings."),))
    rows = tuple((f.severity, f.code, _clip(f.message, 160), f.node or f.edge) for f in result.findings[:50])
    content: list = [vis.ActivityTable(("Severity", "Code", "Message", "Element"), rows)]
    if result.by_code:
        content.append(vis.ActivityTable(("Code", "Findings"), tuple((k, str(v)) for k, v in result.by_code.items())))
    return vis.ActivityPresentation("Validate knowledge base", summary, tuple(content))


def publish(result: KBPublishResult) -> vis.ActivityPresentation:
    return vis.ActivityPresentation("Publish knowledge base", f"{result.bytes} bytes", (vis.ActivityText(result.path),))


def _arguments(names: tuple[str, ...]) -> Callable[[tuple, dict], tuple]:
    def describe(args: tuple, kwargs: dict) -> tuple:
        # A bound method can arrive with its instance first; skip it.
        if args and not isinstance(args[0], (str, int, float, bool)):
            args = args[1:]
        values = dict(zip(names, args))
        values.update({key: value for key, value in kwargs.items() if key in names})
        shown = ", ".join(f"{key}={_clip(repr(values[key]), 200)}" for key in names if key in values)
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
