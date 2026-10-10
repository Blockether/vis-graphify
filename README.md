# vis-graphify

Code knowledge graphs for [Vis](https://github.com/Blockether/vis), built with the
[Blockether fork of graphify](https://github.com/Blockether/graphify). The fork adds full Clojure
support over [Blockether/tree-sitter-clojure](https://github.com/Blockether/tree-sitter-clojure):
namespaces, vars, `:require` aliases, cross-file calls, protocols, records and Clojure 1.12/1.13 syntax.

## When to use

- You want to know who calls a function, across files: use `graphify.explain`.
- You plan a change and want to see what it can break: use `graphify.affected`.
- You want to see how two parts of the code connect: use `graphify.shortest_path`.
- You join an unknown codebase and want its central code first: use `graphify.hubs`.

The graph comes from tree-sitter syntax trees. A build makes no LLM call and sends no code anywhere.

## Install

```bash
vis-agent extension install Blockether/vis-graphify --trust
```

Vis prepares a private environment with graphify and the Clojure grammar. Run `/reload` in a session.

## Use

In a Vis session, ask for the graph work in chat, for example "who calls `run-tests` in this project?".
The agent uses these tools in its Python sandbox:

| Call | Result |
|---|---|
| `graphify.build(root=".")` | Creates or updates `<root>/graphify-out/graph.json`. Later builds re-extract only changed files. |
| `graphify.status(root=".")` | Node, edge and file counts, and the build time. |
| `graphify.search("gateway.*client")` | Nodes whose label, id or file matches the regex. |
| `graphify.explain("my.ns/run")` | The node, what it calls and what calls or contains it. |
| `graphify.shortest_path("a", "b")` | The shortest chain of edges between two nodes. |
| `graphify.hubs(limit=20)` | The most called nodes. |
| `graphify.affected("my.ns/run", depth=2)` | Callers, importers and users, with their files. |
| `graphify.query("how does a session start")` | graphify's own keyword and breadth-first walk, as text. |

You can name a node by its id, its label (`run()`), its bare name (`run`) or its Clojure name
(`my.ns/run`). If a name matches more than one node, the error lists the candidate ids.

Add `graphify-out/` to your `.gitignore`.

## Development

```bash
uv sync
uvx ruff format --check . && uvx ruff check .
uv run --with pytest pytest tests -q
```

## License

Apache-2.0. graphify is MIT-licensed; see `NOTICE`.
