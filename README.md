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
- You want to keep concepts, notes and documents next to the code graph, check them against an
  ontology and see them in the browser: use `graphify.write`, `graphify.ingest` and
  `graphify.serve`. `graphify.search` and `graphify.explain` then show them next to the code. See
  [Knowledge base](#knowledge-base).
- Your team runs a knowledge base server, and you want the agent to read and edit it while the code
  tools keep the local graph: give its URL as `source=`. See
  [Use a hosted knowledge base](#use-a-hosted-knowledge-base).
- You want a local copy of that server, for example to read it offline: use `graphify.snapshot`.
  See [Copy a knowledge source](#copy-a-knowledge-source).
- You want every session to use that server without a question for the token: save it in Vis
  Settings. See [Save the server in Settings](#save-the-server-in-settings).

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
| `graphify.search("gateway.*client")` | Code nodes whose label, id or file matches the regex, then the knowledge nodes and documents that match its words. |
| `graphify.explain("my.ns/run")` | The node, what it calls and what calls or contains it, with the notes and documents about it. A knowledge node or a document works too. |
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
- `moved_nodes` and `kb`: the graph nodes with a new id, and the knowledge bases that got the new
  ids. See [Keep knowledge base links after a rename or a move](#keep-knowledge-base-links-after-a-rename-or-a-move).

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

The result has `diff`, `created`, `deleted`, `renames`, `notes`, `conflicts`, `moved_nodes` and
`kb`, as for [a rename](#keep-knowledge-base-links-after-a-rename-or-a-move). The call writes
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

The knowledge base adds your concepts, documents and edges to the code graph, and checks them
against an ontology. You and the agent edit the same store, and each change is undoable.
`graphify.search` and `graphify.explain` show this knowledge next to the code. The code itself
always comes from the local tree-sitter graph, also in `affected`, `rename` and `move`.

A knowledge source is one of these:

- `local`: the store of the project in `<root>/graphify-out/kb/`. This is the default.
- The folder or `graph.json` of another store on this machine, for example `../team-notes`.
- A snapshot folder in `graphify-out/snapshots/`. See [Copy a knowledge source](#copy-a-knowledge-source).
- A server URL, for example `https://kb.example.com`. See
  [Use a hosted knowledge base](#use-a-hosted-knowledge-base).

Each knowledge call takes `source=`. Without it, the call uses the
[configured server](#save-the-server-in-settings), else `local`. Without a source, `search` and
`explain` read `local` only when it holds knowledge. So a project without knowledge starts no
server. When a knowledge source does not answer, they still give the code, and `note` tells why the
knowledge is missing.

```python
graphify.write([
    {"op": "add_node", "label": "Billing", "class": "concept"},
    {"op": "add_edge", "source": "kb:billing", "target": "src_billing_invoice", "relation": "uses"},
    {"op": "add_document", "title": "Billing rules", "body": "[[Billing]] runs nightly."},
])
print(graphify.search("billing"))               # code nodes, then knowledge nodes and documents
print(graphify.explain("Billing"))              # a knowledge node: by id, document id or label
print(graphify.ingest("docs/billing.pdf"))      # text mode: no model call
print(graphify.ingest("notes.md", mode="llm"))  # the model adds nodes and edges
print(graphify.validate())                      # ontology findings, errors first
graphify.undo()                                 # revert the latest change of this session
print(graphify.publish())                       # static graphify-out/kb.html
```

- A batch in `write` is one change: if one operation fails, the store applies none of them.
- `undo()` reverts the latest `write` or `ingest` of this session, also when other people
  changed the store after it. Give `seq=` to revert another change from the history.
- `explain` of a document gives its title, tags and text, and the nodes that it links.
- A store on this machine gets a server for these calls. The first call starts `graphify kb serve`
  for it on a free loopback port. The server keeps running after the session, and the next session
  uses it.
- `graphify.serve()` gives the URL of that server. Open the URL in a browser for the admin UI: a
  graph map, node inspector, ontology, consistency findings, documents and change history. Give
  `port=`, `host=` or `read_only=True` to choose them.
- `graphify.serve(stop=True)` stops that server. It also works after a restart of the gateway.

The [knowledge base guide](https://github.com/Blockether/graphify/blob/v8/docs/kb.md) describes the
HTTP API and the ontology.

### Keep knowledge base links after a rename or a move

A rename or a move gives a code node a new id. Your edits, edges and document links of that node
then go to the new id. `rename`, `move` and `move_file` do this after each written change:

- `moved_nodes` lists each node with a new id, as `old id -> new id`.
- The project store `graphify-out/kb/` gets the new ids through `graphify kb move`.
- The [configured server](#save-the-server-in-settings) gets them too, over HTTP, unless it serves
  the project store. A snapshot gets nothing: the next `snapshot` copies the moved records.
- `kb` has one line for each knowledge base, for example
  `graphify-out/kb: 1 moved, 0 pending, 0 with no records at the old id`.

A pending move waits until that graph has the new code, for example after its next rebuild. After a
file move, graphify often finds the node by its file, label and class first. The links are then at
the new id already, and the line counts them as no records at the old id. A line with `not updated`
gives the error, and the code change stays written.

### Use a hosted knowledge base

If your team runs `graphify kb serve` on a server, give its URL as `source=`. To use it in every call
without a source, [save it in Settings](#save-the-server-in-settings). The code tools keep the local
graph.

```python
url = "https://kb.example.com"
print(graphify.search("billing", source=url))
graphify.write([{"op": "add_node", "label": "Billing", "class": "concept"}], source=url)
print(graphify.search("billing", source="local"))  # the store of the project
```

When the server needs a token, Vis shows a masked form in the TUI or the Companion. The form checks
the token with the server before it closes. The token stays in memory. It does not go into the
conversation, the logs or a result. Each session asks once for each server. If the server refuses
the token later, for example after a token change, the next call asks again. To connect without the
form, see [Save the server in Settings](#save-the-server-in-settings).

- A URL without a scheme uses https, for example `kb.example.com`. A loopback address, such as
  `127.0.0.1:7878`, uses http.
- If the server redirects to https on the same host, graphify uses the new address. It never
  follows a redirect to another host, so the token goes only to the server that you named.
- Each session has its own tokens and its own undo history.
- `ingest` sends the file content, so you can add files from your machine. `publish` first
  copies the store of the server, then writes `graphify-out/kb-<host>-<port>.html` on your machine.

#### Save the server in Settings

Save the server and its token once in Vis Settings. Then every call without a source uses that
server, and no form asks for the token:

1. In Settings, open **Extensions** in the **Tools** section, then the **vis-graphify** group.
2. In **Knowledge base server**, type the address, for example `https://kb.example.com`, and save.
3. In **Knowledge base token**, select **Set**, type the token and save.

Settings shows the token only as **Saved** or **Not set**. Vis keeps it in `~/.vis/state.yml`, which
only your user account can read. graphify sends this token only to the server in **Knowledge base
server**. A project file cannot change these two settings. They win over the environment variables
in the next section.

Save both settings. A saved token without a saved server has no effect: graphify then uses the
server and the token of the environment variables.

If your Vis does not show these settings, it is older than this feature. Then use the environment
variables.

#### Connect without the form

To connect without the form and without Settings, give the server and its token in two
environment variables:

- `GRAPHIFY_KB_URL` is the server address. The knowledge calls without `source` use it. The agent
  sees this address, but never the token. Without it, the default source is `local`.
- `GRAPHIFY_KB_TOKEN` is the token. graphify sends it only to that server. Another server that a
  call names with `source` does not get it.
- `serve(host=...)` with an address other than loopback gives the token to the server that it
  starts. That server then needs the token, and its admin UI asks for it in a sign-in form. A
  loopback server gets no token.

Vis gives these variables to graphify when it loads the extension. Set them in one of these places:

- The `environment:` block of `~/.vis/config.yml`, for all your projects, or of the project's
  `vis.yml`, for that project only. Use a reference, so that the file does not hold the token:

  ```yaml
  environment:
    GRAPHIFY_KB_URL: {literal: "https://kb.example.com"}
    GRAPHIFY_KB_TOKEN: {keychain: graphify-kb}
  ```

  On macOS, save the token with `security add-generic-password -s graphify-kb -a "$USER" -w`. The
  command asks for the token, so it does not go into the shell history. A team can commit these
  lines in the project's `vis.yml`. Each person then saves their own token under the same name.

- The `.env` file of the project. Keep that file out of Git.
- The environment of the process that starts the Vis gateway. A running gateway keeps its
  environment, so restart the gateway after a change.

After a change in a file, run `/reload`.

The files of a project win over your own configuration: its `vis.yml` over `~/.vis/config.yml`, and
its `.env` over the process environment. So a project can point `GRAPHIFY_KB_URL` at another
server, and graphify then sends your token there. Before you open a project that you did not
write, check that it does not set `GRAPHIFY_KB_URL`. Settings do not have this risk.

#### Host the server

To host the server, keep it on loopback and put a TLS reverse proxy in front of it:

```bash
export GRAPHIFY_KB_TOKEN="$(openssl rand -hex 24)"  # at least 16 characters; keep it in a secret store
graphify kb serve graphify-out/graph.json --allow-host kb.example.com
```

- The server listens on `127.0.0.1:7878`. Point the proxy for `kb.example.com` at that address.
- With a token, every API call except `/api/health` needs the token.
- `--allow-host` names the public hostname. The server refuses requests for other host names.
- The server reads the token from `GRAPHIFY_KB_TOKEN`, so the token does not show in the process list.
- The stores of this extension always use SQLite. To use another store backend on a server that you
  host, start it with `graphify kb serve --store NAME`. See
  [Use another store](https://github.com/Blockether/graphify/blob/v8/docs/kb.md#use-another-store).

### Copy a knowledge source

`graphify.snapshot(source)` copies a source into `graphify-out/snapshots/<name>/` of the project:
the graph, the ontology, the knowledge records and the documents. Read the copy without the server,
for example offline or to compare it with the server later.

```python
snap = graphify.snapshot("https://kb.example.com")  # graphify-out/snapshots/kb.example.com
print(graphify.search("billing", source=snap.name))
```

- A snapshot is read-only. Write to the source that it copies, then copy that source again.
- A new snapshot with the same name replaces the old copy. Give `name=` to keep more copies.
- Give `assets=True` to also copy the files of the documents, such as images and PDFs.
- The copy holds no token. The server needs graphify v0.9.93-blockether.17 or later.

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
