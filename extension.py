"""Vis entrypoint. The tools themselves live in vis_graphify."""

from typing import Literal

import blockether.vis.extension as vis

from vis_graphify import presentation
from vis_graphify.tools import GraphifyTools

VERSION = "0.3.1"

_Tag = Literal["observation", "mutation"]


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
_bind("affected", "Find affected code", presentation.affected, ("node", "root", "depth"))
_bind("references", "Find code references", presentation.references, ("node", "root"))
_bind("rename", "Rename symbol", presentation.rename, ("node", "new_name", "root", "apply"), tag="mutation")
_bind("domain", "Map domain flows", presentation.domain, ("root", "plugins"), tag="mutation", show_start=True)

PROMPT = """graphify surface active: a code knowledge graph (tree-sitter AST, no LLM) of the workspace.
  graphify.build(root=".")                    create or update <root>/graphify-out/graph.json
  graphify.status(root=".")                   size and age of the graph
  graphify.search(pattern) · graphify.explain(node) · graphify.hubs()
  graphify.shortest_path(source, target) · graphify.affected(node, depth=2)
  graphify.references(node)                   every exact site of a symbol, with line, column and role
  graphify.rename(node, new_name, apply=False) rename a function, method, class or var everywhere; a preview until apply=True
  graphify.domain(root=".", plugins=None)     commands, queries, events, flows and Malli schemas -> DOMAIN.md
references() and rename() work in every language with a tree-sitter grammar: Python, JavaScript, TypeScript, Go, Rust, Java, Clojure and more.
Call graphify.build() once before the first query, and again after edits that you make without rename(); it re-extracts only changed files.
Name a node by id, label (`run-tests()`), bare name or Clojure `ns/name`. An ambiguous name raises an error that lists the candidate ids.
Use the graph to find callers, dependents and paths across files; read the code itself before you edit it.
To rename a symbol, use graphify.rename(), not hand edits: call it without apply, read its conflicts, diff and mentions, then call it with apply=True. It updates the graph.
graphify.domain() reports calls to commands or queries that no service registers, call data that the :consumes schema rejects and broken Malli schemas. Read DOMAIN.md for the diagrams.
Add a framework as a plugin file in <root>/.graphify/plugins/ or pass its path in `plugins`.
Results are typed frozen objects."""

vis.register_extension(
    vis.Extension(
        name="vis-graphify",
        description="Code knowledge graphs with graphify: build, search, explain, paths, hubs, change impact, exact references and safe renames in every tree-sitter language, and domain flows (edd-core commands, queries, events, Malli schemas).",
        version=VERSION,
        alias="graphify",
        symbols=[vis.Symbol(GraphifyTools(workspace_root=vis.workspace_root), name="graphify")],
        prompt=PROMPT,
    )
)
