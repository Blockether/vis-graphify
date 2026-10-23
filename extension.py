"""Vis entrypoint. The tools themselves live in vis_graphify."""

import os
from typing import Literal

import blockether.vis.extension as vis

from vis_graphify import presentation
from vis_graphify.kb import KBSearchResult, normalize_url
from vis_graphify.tools import GraphifyTools, HubsResult, ImpactResult, ReferencesResult, SearchResult

VERSION = "0.11.1"

_Tag = Literal["observation", "mutation"]

# `for x in result` and `len(result)` read the main list of these results.
vis.sequence(field="nodes")(SearchResult)
vis.sequence(field="references")(ReferencesResult)
vis.sequence(field="impacts")(ImpactResult)
vis.sequence(field="hubs")(HubsResult)
vis.sequence(field="hits")(KBSearchResult)


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

_bind(
    "kb_serve",
    "Start knowledge base",
    presentation.kb_server,
    ("root", "port", "host", "read_only"),
    tag="mutation",
    show_start=True,
)
_bind("kb_stop", "Stop knowledge base", presentation.kb_stopped, ("url",), tag="mutation")
_bind("kb_connect", "Connect knowledge base", presentation.kb_server, ("url",), show_start=True)
_bind("kb_search", "Search knowledge base", presentation.kb_search, ("query", "url", "cls"))
_bind("kb_node", "Read knowledge node", presentation.kb_node, ("node", "url"))
_bind("kb_write", "Write knowledge base", presentation.kb_change("Write knowledge base"), ("url",), tag="mutation")
_bind(
    "kb_ingest",
    "Ingest document",
    presentation.kb_ingest,
    ("source", "mode", "title", "url"),
    tag="mutation",
    show_start=True,
)
_bind("kb_validate", "Validate knowledge base", presentation.kb_validate, ("url", "limit"), show_start=True)
_bind(
    "kb_undo", "Undo knowledge change", presentation.kb_change("Undo knowledge change"), ("url", "seq"), tag="mutation"
)
_bind("kb_publish", "Publish knowledge base", presentation.kb_publish, ("url", "path"), tag="mutation")

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
Also: graphify.explain(node) · shortest_path(a, b) · search(pattern) · hubs() · domain() (edd-core flows -> DOMAIN.md).
Knowledge base (ontology, documents, undoable edits, admin UI in the browser), shared with people over HTTP:
  print(graphify.kb_serve())    start or reuse `graphify kb serve` for the project graph; give the user its URL
  print(graphify.kb_connect("https://kb.example.com"))  use a hosted server; a masked form asks the person for its token, so never ask for it in chat
  graphify.kb_search(q) · kb_node(id) · kb_validate()   read nodes, documents and ontology findings
  graphify.kb_write([{"op": "add_node", "label": "Billing", "class": "concept"}, {"op": "add_edge", ...}])  one undoable change; doc("graphify.kb_write") lists the ops
  graphify.kb_ingest("docs/spec.pdf", mode="text"|"llm") · kb_undo() · kb_publish() (static kb.html)"""


def _prompt(env) -> str:
    """The prompt, with the configured knowledge base server when `$GRAPHIFY_KB_URL` names a valid one."""
    try:
        url = normalize_url(os.environ.get("GRAPHIFY_KB_URL", ""))
    except ValueError:
        return PROMPT
    return f"{PROMPT}\n  The configured server is {url}: the kb_* calls use it without kb_connect, until kb_serve or kb_connect names another server."


def _ask_kb_token(url: str, check) -> str | None:
    """Ask the person for the token of a knowledge base server in a masked form."""
    answer = vis.ask(
        "Knowledge base token",
        [vis.password("token", label="Token", description=f"The token of {url}.", is_required=True, validate=check)],
        description="The token stays out of the conversation, the logs and the model.",
        submit_label="Connect",
    )
    if not answer:
        return None
    try:
        return answer.reveal("token")
    finally:
        vis.forget(answer["token"])


def _kb_secret(token: str) -> str:
    """The token itself, or the plaintext behind a `vis-secret:` handle of a masked form."""
    if not token.startswith("vis-secret:"):
        return token
    value = vis.reveal(token)
    if not value:
        raise ValueError("The vis-secret: handle is unknown or forgotten. Ask for the token again.")
    return value


vis.register_extension(
    vis.Extension(
        name="vis-graphify",
        description="Code knowledge graphs for Vis with graphify: search, change impact, exact references, safe renames and moves, domain flows, and a knowledge base server with ontology, documents and undoable edits.",
        version=VERSION,
        alias="graphify",
        symbols=[
            vis.Symbol(
                GraphifyTools(workspace_root=vis.workspace_root, kb_ask_token=_ask_kb_token, kb_secret=_kb_secret),
                name="graphify",
            )
        ],
        prompt=_prompt,
        env=["GRAPHIFY_KB_URL", "GRAPHIFY_KB_TOKEN"],
    )
)
