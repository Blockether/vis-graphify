# vis-graphify

Code knowledge graphs for [Vis](https://github.com/Blockether/vis), built with the
[Blockether fork of graphify](https://github.com/Blockether/graphify). The fork adds full Clojure
support over [Blockether/tree-sitter-clojure](https://github.com/Blockether/tree-sitter-clojure):
namespaces, vars, `:require` aliases, cross-file calls, protocols, records and Clojure 1.12/1.13 syntax.

## When to use

- You want to know who calls a function, across files: use `graphify.explain`.
- You plan a change and want to see what it can break: use `graphify.affected`.
- You want to rename a function, method, class or Clojure var everywhere without touching locals,
  fields, strings or other symbols with the same name: use `graphify.rename`. See
  [Rename a symbol](#rename-a-symbol).
- You want to add a parameter to a function and pass a value at every call: use
  `graphify.add_parameter`. See [Add a parameter](#add-a-parameter).
- You want to move a function or a type to another file, or move a file to a new module or
  namespace, and fix every import: use `graphify.move` or `graphify.move_file`. See
  [Move code](#move-code).
- You want to see how two parts of the code connect: use `graphify.shortest_path`.
- You join an unknown codebase and want its central code first: use `graphify.hubs`.
- You want to see how commands, queries and events flow between your services, or find calls to
  commands that no service registers: use `graphify.domain`. See [Domain flows](#domain-flows).
- You want to edit the graph together with an agent: add concepts and documents, check them against
  an ontology and see the result in the browser. Use `graphify.kb_serve`. See
  [Knowledge base](#knowledge-base).
- Your team runs a knowledge base server, and you want the agent to read and edit it: use
  `graphify.kb_connect`. See [Use a hosted knowledge base](#use-a-hosted-knowledge-base).

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
| `graphify.build(root=".")` | Creates or updates `<root>/graphify-out/graph.json`. Other calls do this for you when the graph is missing or a source file changed. |
| `graphify.status(root=".")` | Node, edge and file counts, and the build time. |
| `graphify.search("gateway.*client")` | Nodes whose label, id or file matches the regex. |
| `graphify.explain("my.ns/run")` | The node, what it calls and what calls or contains it. |
| `graphify.shortest_path("a", "b")` | The shortest chain of edges between two nodes. |
| `graphify.hubs(limit=20)` | The most called nodes. |
| `graphify.affected("my.ns/run", within="src/api/")` | Every caller, importer and user, across all steps, with distance and file. `within` keeps only one folder or glob. `depth=2` stops after two steps. |
| `graphify.references("my.ns/run")` | Every exact site of a symbol: file, line, column and role. |
| `graphify.rename("my.ns/run", "start", apply=True)` | Renames the symbol everywhere and updates the graph. Without `apply`, it only returns the diff. |
| `graphify.add_parameter("Total", "currency string", '"EUR"', apply=True)` | Adds a last parameter to the definition and the argument to every call. Without `apply`, it only returns the diff. |
| `graphify.move("my.ns/run", "src/my/runner.clj", apply=True)` | Moves one top-level definition to another file, also a new one. Every use, import and require follows. |
| `graphify.move_file("src/my/ns.clj", "src/my/core.clj", apply=True)` | Moves a file and renames its module or namespace at every use. For Clojure, the test namespace moves too. |
| `graphify.domain(root=".")` | The domain model: services, commands, queries, events, links, schemas and findings. Writes `DOMAIN.md`. |

You can name a node by its id, its label (`run()`), its bare name (`run`) or a qualified name
(`my.ns/run`, `orders.Order`, `Ledger.post` or `crate::util::sum`), or by its file and name
(`src/util.rs::checksum`). If a name matches more than one
node, the error lists each candidate id with its file and line.

`print(result)` shows a short summary with `file:line` locations. Read a field, such as `diff`, for the details.
The graph build writes `graphify-out/.gitignore`, so git and `grep` skip the graph files.

## Rename a symbol

Ask in chat, for example "rename `parse_config` to `load_config`". The agent calls
`graphify.rename("parse_config()", "load_config", apply=True)`. When the rename has a conflict,
the call writes nothing. The result has these fields:

- `diff`: the change in each file;
- `conflicts`: why the rename is unsafe, if it is. Examples are a file that already uses the new
  name, a method that overrides a base class or implements a trait, or a common method name such as
  `get` on receivers of unknown type;
- `mentions`: other code symbols that spell the old name. They stay unchanged; check them.
- `text_mentions`: the count of strings, comments and keywords that spell the old name. They stay unchanged.

Without `apply=True`, the call is a preview and writes nothing. A file that changed after the plan stops the write.

The rename works in every language with a tree-sitter grammar in graphify: Python, JavaScript,
TypeScript, Go, Rust, Java, C#, Kotlin, Ruby, PHP, Clojure and more. It follows imports, aliases,
re-exports and declared receiver types, and it skips parameters, locals and fields with the same name.
For Clojure, it follows `:as`, `:refer`, `:refer :all` and `:rename`, and changes `declare`,
`defmethod`, protocol implementations, `#'var`, `resolve`, syntax-quote and `->Rec` factories.
`graphify.references()` lists the same sites without a change.

## Add a parameter

Ask in chat, for example "add a `currency string` parameter to `pricing.Total` and pass `"EUR"`".
The agent calls `graphify.add_parameter("pricing/pricing.go::Total", "currency string", '"EUR"', apply=True)`.
The call finds the calls with the same exact resolution as `graphify.references()`. A method or a
function in another package with the same name stays unchanged.

- Write the parameter as the language writes it: `currency string` (Go), `currency: str` (Python),
  `currency: string` (TypeScript) or `currency` (Clojure).
- The parameter goes last in the definition, and the argument goes last in each call.
- A multi-line call with a trailing comma gets the argument on its own line.

The call writes nothing when a site blocks the change. Examples are a function that is passed as a
value, not called, or a Python list that ends with `*args`, `**kwargs` or a default. In Python, pass
`name=value` after a default. Then change the blocked sites by hand.

## Move code

Ask in chat, for example "move `slugify` from `app.util` to a new file `app/text.py`". The agent
calls `graphify.move("app/util.py::slugify", "app/text.py", apply=True)`. To move a whole file, the
agent calls `graphify.move_file("src/app/util.clj", "src/app/text/core.clj", apply=True)`.

- The move works for Clojure, Python, JavaScript, TypeScript, Go and Rust. It also works for Java,
  Kotlin, Scala, Groovy, C#, VB.NET, PHP, Swift, Elixir, Fortran, Erlang, OCaml, Pascal and Apex.
  The languages that load files by path work too: C, C++, Objective-C, Ruby, shell, PowerShell,
  Julia, Verilog, Lua, Zig, R, SQL, Common Lisp, Robot Framework, COBOL, Dart, Solidity, Terraform
  and others.
- Every import, require, `mod` declaration and qualified use follows the code.
- The moved definition takes the imports that it needs.
- For Clojure, `move_file` changes the `ns` form. It also moves the test namespace `x-test` of
  namespace `x`. Give `tests=False` to keep the tests in place.
- For Rust, `impl` blocks move with their type. A private item that the new module needs becomes
  `pub(crate)`. `notes` lists these changes.

The result has `diff`, `created`, `deleted`, `renames`, `notes` and `conflicts`. The call writes
nothing when a conflict blocks the move. Examples are a name that the target file already defines,
a target file that exists, or an import cycle. Without `apply=True`, the call is a preview. A
written move updates the graph and runs the project check, as `rename` does.

## Domain flows

`graphify.domain()` reads the framework registrations of your Clojure services and connects them.
The built-in `edd-core` plugin reads [edd-core](https://github.com/alpha-prosoft/edd-core) commands,
queries, events, effects and Malli schemas. The call writes these files to `<root>/graphify-out/domain/`:

- `DOMAIN.md`: a summary, findings, Mermaid flowcharts for the system and each service, a sequence
  diagram for each entry point, and the schemas;
- `domain.json`: the full model;
- `schemas/`: one JSON Schema for each Malli schema.

The result lists the findings, errors first. For example, `missing-command` means that code calls a
command that the target service does not register. `payload-invalid` means that a call sends data
that the full `:consumes` schema of its target rejects. Codes that start with `schema-` mark a broken
Malli schema, such as an unknown type, `:min` above `:max` or a regex that cannot compile. `DOMAIN.md`
lists these problems under each schema. `graphify.build()` also adds the domain elements
to the graph, so `graphify.explain("domain:command:orders-svc:place-order")` shows the handler.

To read another framework, write a domain plugin: a Python file with a `plugin` object that has a
`name` and an `extract(corpus, model)` method. The
[plugin guide](https://github.com/Blockether/graphify/blob/v8/docs/domain.md#write-a-plugin)
describes the contract. Make the plugin available in one of these ways:

- Put the file in `<root>/.graphify/plugins/`. graphify finds it for that project.
- Pass its path or name in `plugins`, for example `graphify.domain(plugins=["tools/jobs.py"])`.
- Add it to `src/vis_graphify/domain_plugins/` in this repository. Every Vis user of the
  extension then gets it.

## Knowledge base

`graphify.kb_serve()` starts `graphify kb serve` for the project graph and returns its URL. Open the
URL in a browser for the admin UI: a graph map, node inspector, ontology, consistency findings,
documents and change history. You and the agent edit the same graph. The server writes each change
to `<root>/graphify-out/kb/`, and each change is undoable.

```python
print(graphify.kb_serve())
graphify.kb_write([
    {"op": "add_node", "label": "Billing", "class": "concept"},
    {"op": "add_edge", "source": "kb:billing", "target": "src_billing_invoice", "relation": "uses"},
    {"op": "add_document", "title": "Billing rules", "body": "[[Billing]] runs nightly."},
])
print(graphify.kb_ingest("docs/billing.pdf"))      # text mode: no model call
print(graphify.kb_ingest("notes.md", mode="llm"))  # the model adds nodes and edges
print(graphify.kb_validate())                      # ontology findings, errors first
graphify.kb_undo()                                 # revert the latest change
print(graphify.kb_publish())                       # static graphify-out/kb.html
```

A batch in `kb_write` is one change: if one operation fails, the server applies none of them. Read
with `kb_search` and `kb_node`. The other `kb_*` calls use the server of the latest `kb_serve` or
`kb_connect`. To use another server in one call, give `url`. `kb_stop()` stops a server that
`kb_serve` started. The [knowledge base guide](https://github.com/Blockether/graphify/blob/v8/docs/kb.md)
describes the HTTP API and the ontology.

### Use a hosted knowledge base

If your team runs `graphify kb serve` on a server, connect to it once. The other `kb_*` calls then
use that server and its token.

```python
print(graphify.kb_connect("https://kb.example.com"))
print(graphify.kb_search("billing"))
```

When the server needs a token, Vis shows a masked form in the TUI or the Companion. The form checks
the token with the server before it closes. The token stays in memory. It does not go into the
conversation, the logs or a result.

- To connect without the form, set `GRAPHIFY_KB_TOKEN` before Vis starts. You can also give
  `token=`, but then the token shows in the conversation.
- A URL without a scheme uses https, for example `kb.example.com`. A loopback address, such as
  `127.0.0.1:7878`, uses http.
- If the server redirects to https on the same host, `kb_connect` uses the new address. It never
  follows a redirect to another host, so the token goes only to the server that you named.
- To make a server the default without `kb_connect`, set `GRAPHIFY_KB_URL`.
- `kb_ingest` sends the file content, so you can add files from your machine. `kb_publish` writes
  `kb.html` on the server machine.

To host the server, keep it on loopback and put a TLS reverse proxy in front of it:

```bash
export GRAPHIFY_KB_TOKEN="$(openssl rand -hex 24)"  # at least 16 characters; keep it in a secret store
graphify kb serve graphify-out/graph.json --allow-host kb.example.com
```

- The server listens on `127.0.0.1:7878`. Point the proxy for `kb.example.com` at that address.
- With a token, every API call except `/api/health` needs the token.
- `--allow-host` names the public hostname. The server refuses requests for other host names.
- The server reads the token from `GRAPHIFY_KB_TOKEN`, so the token does not show in the process list.

## Development

```bash
uv sync
uvx ruff format --check . && uvx ruff check .
uv run --with pytest pytest tests -q
```

## Benchmark

[`bench/`](bench/README.md) measures extraction, queries and change tracking on real Clojure
projects against clj-kondo. On the Vis sources (235 files), definitions have a recall of 0.999,
references a recall of 0.996, and `explain()` finds 99.8 percent of the callers in a few milliseconds.
A full build takes about 4 s, and an update after a one-file change about 5 s.

## License

Apache-2.0. graphify is MIT-licensed; see `NOTICE`.
