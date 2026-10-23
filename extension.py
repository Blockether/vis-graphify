"""Vis entrypoint. The tools themselves live in vis_graphify."""

from typing import Literal

import blockether.vis.extension as vis

from vis_graphify import presentation
from vis_graphify.tools import GraphifyTools, HubsResult, ImpactResult, ReferencesResult, SearchResult

VERSION = "0.6.0"

_Tag = Literal["observation", "mutation"]

# `for x in result` and `len(result)` read the main list of these results.
vis.sequence(field="nodes")(SearchResult)
vis.sequence(field="references")(ReferencesResult)
vis.sequence(field="impacts")(ImpactResult)
vis.sequence(field="hubs")(HubsResult)


def _bind(name, label, build, arguments=(), *, tag: _Tag = "observation", show_start=False):
    """Attach one Activity presentation to a method of GraphifyTools."""
    setattr(
        GraphifyTools,
        name,
        vis.method(
            tag=tag,
            activity=vis.Activity(
                label=label,
                render=presentation.renderer(label, build, arguments),
                show_start=show_start,
            ),
        )(getattr(GraphifyTools, name)),
    )


_bind(
    "build",
    "Build code graph",
    presentation.build,
    ("root", "force", "cluster", "timeout_s", "plugins"),
    tag="mutation",
    show_start=True,
)
_bind("status", "Check code graph", presentation.status, ("root",))
_bind("search", "Search code graph", presentation.search, ("pattern", "root"))
_bind("explain", "Explain code node", presentation.explain, ("node", "root"))
_bind("shortest_path", "Find code path", presentation.shortest_path, ("source", "target", "root"))
_bind("hubs", "List code hubs", presentation.hubs, ("root", "limit", "relation"))
_bind("affected", "Find affected code", presentation.affected, ("node", "root", "depth", "within"))
_bind("references", "Find code references", presentation.references, ("node", "root"))
_bind(
    "rename",
    "Rename symbol",
    presentation.rename,
    ("node", "new_name", "root", "apply"),
    tag="mutation",
    show_start=True,
)
_bind(
    "add_parameter",
    "Add parameter",
    presentation.add_parameter,
    ("node", "parameter", "argument", "root", "apply"),
    tag="mutation",
    show_start=True,
)
_bind(
    "move",
    "Move definition",
    presentation.move,
    ("node", "to", "root", "apply"),
    tag="mutation",
    show_start=True,
)
_bind(
    "move_file",
    "Move file",
    presentation.move_file,
    ("path", "new_path", "root", "tests", "apply"),
    tag="mutation",
    show_start=True,
)
_bind("domain", "Map domain flows", presentation.domain, ("root", "plugins"), tag="mutation", show_start=True)

PROMPT = """graphify: an exact code graph of the workspace (tree-sitter) for Python, JS, TS, Go, Rust, Java, Clojure and more. It builds and updates itself.
Make one graphify call the first step, before any ls, cat, grep or shell:
  print(graphify.affected(node, within="src/api/"))  what a change can break, across all steps
  print(graphify.references(node))                   every exact site of a symbol
  print(graphify.rename(node, new_name, apply=True)) rename a function, method, class, type or var everywhere
  print(graphify.add_parameter(node, "currency string", '"EUR"', apply=True))  add a parameter and an argument at every call
  print(graphify.move(node, "src/text/core.clj", apply=True))  move a definition to another file, also a new one; every use follows
  print(graphify.move_file("src/app/util.py", "src/app/text.py", apply=True))  move a file and rename its module or ns everywhere, Clojure tests too
Results are exact and complete: strings, comments and other symbols with the same name stay out. Do not open files to confirm them.
affected lists the dependent functions in `r.functions` and the private ones in `r.private`. Write an answer from these fields in the same block.
rename, add_parameter, move and move_file write nothing on a conflict. After a write, they run the project check. When the result says `passed`, reply at once.
Name a node by `pkg.Name`, `Class.method`, `ns/name`, `crate::mod::name` or bare name. When the task names the file, use `src/money/format.ts::name`.
Also: graphify.explain(node) · shortest_path(a, b) · search(pattern) · hubs() · domain() (edd-core flows -> DOMAIN.md)."""

vis.register_extension(
    vis.Extension(
        name="vis-graphify",
        description="Code knowledge graphs with graphify: build, search, explain, paths, hubs, change impact, exact references, safe renames and moves of definitions and files, and domain flows (edd-core commands, queries, events, Malli schemas).",
        version=VERSION,
        alias="graphify",
        symbols=[vis.Symbol(GraphifyTools(workspace_root=vis.workspace_root), name="graphify")],
        prompt=PROMPT,
    )
)
