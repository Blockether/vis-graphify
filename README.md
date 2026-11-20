# vis-graphify

Build a code knowledge graph in [Vis](https://github.com/Blockether/vis) with graphify. Search code, trace the effect of changes, make exact refactors, and keep concepts and documents in a knowledge base.

## Install

```bash
vis-agent extension install Blockether/vis-graphify --trust
```

Run `/reload` in your Vis session.

## Use

Ask in chat, for example, "Who calls `run-tests` in this project?" Graphify builds the code graph from tree-sitter syntax trees. A build makes no model call and sends no code.

| Tool | Result |
|---|---|
| `graphify.build` | Create or update the local code graph. |
| `graphify.status` | Show node, edge, and file counts and the build time. |
| `graphify.search` | Find code, knowledge nodes, and documents. |
| `graphify.explain` | Show a node, its connections, and its notes. |
| `graphify.shortest_path` | Find the shortest link between two nodes. |
| `graphify.hubs` | List the nodes with the most calls. |
| `graphify.affected` | Find callers, importers, and other users of a symbol. |
| `graphify.references` | Find each exact reference to a symbol. |
| `graphify.rename` | Rename a symbol and update its references. |
| `graphify.add_parameter` | Add a parameter and pass a value at each call. |
| `graphify.move` | Move a definition and update its references. |
| `graphify.move_file` | Move a file and rename its module or namespace. |
| `graphify.domain` | Map service commands, queries, and events. |
| `graphify.crap` | Rank Clojure and Python functions by complexity and test coverage. |
| `graphify.write` | Add or edit knowledge records. |
| `graphify.ingest` | Add a document to the knowledge base. |
| `graphify.validate` | Check the knowledge base against its ontology. |
| `graphify.undo` | Reverse a knowledge base edit. |
| `graphify.snapshot` | Copy a knowledge source for offline use. |
| `graphify.publish` | Export the knowledge base as static HTML. |
| `graphify.serve` | Open or stop the knowledge base server. |

Refactor tools show a preview by default. Set `apply=True` to write the changes. See the [code graph reference](docs/code-graph.md) and [knowledge base guide](docs/knowledge-base.md) for options and examples.

## Configuration

To use a hosted knowledge base by default, set **Knowledge base server** and **Knowledge base token** in Vis Settings under **Extensions**, then **vis-graphify**. If your Vis version does not show these settings, see [GRAPHIFY_KB_URL and GRAPHIFY_KB_TOKEN](docs/knowledge-base.md#connect-without-the-form).

## Development

```bash
uv sync
uvx ruff format --check . && uvx ruff check .
uv run --with pytest pytest tests -q
```
