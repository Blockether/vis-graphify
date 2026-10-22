# vis-graphify

Code knowledge graphs for [Vis](https://github.com/Blockether/vis), built with the
[Blockether fork of graphify](https://github.com/Blockether/graphify). The fork adds full Clojure
support over [Blockether/tree-sitter-clojure](https://github.com/Blockether/tree-sitter-clojure):
namespaces, vars, `:require` aliases, cross-file calls, protocols, records and Clojure 1.12/1.13 syntax.

## When to use

- You want to know who calls a function, across files: use `graphify.explain`.
- You plan a change and want to see what it can break: use `graphify.affected`.
- You want to rename a Clojure var everywhere without touching locals, keywords or strings with the
  same name: use `graphify.rename`. See [Rename a Clojure var](#rename-a-clojure-var).
- You want to see how two parts of the code connect: use `graphify.shortest_path`.
- You join an unknown codebase and want its central code first: use `graphify.hubs`.
- You want to see how commands, queries and events flow between your services, or find calls to
  commands that no service registers: use `graphify.domain`. See [Domain flows](#domain-flows).

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
| `graphify.references("my.ns/run")` | Every exact site of a Clojure var: file, line, column and role. |
| `graphify.rename("my.ns/run", "start")` | A rename preview with a diff and conflicts. `apply=True` writes the files. |
| `graphify.query("how does a session start")` | graphify's own keyword and breadth-first walk, as text. |
| `graphify.domain(root=".")` | The domain model: services, commands, queries, events, links, schemas and findings. Writes `DOMAIN.md`. |
| `graphify.domain_plugins(root=".")` | The domain plugins that graphify can load for the project. |

You can name a node by its id, its label (`run()`), its bare name (`run`) or its Clojure name
(`my.ns/run`). If a name matches more than one node, the error lists the candidate ids.

Add `graphify-out/` to your `.gitignore`.

## Rename a Clojure var

Ask in chat, for example "rename `my.ns/run` to `start`". The agent first calls
`graphify.rename("my.ns/run", "start")`. This call writes nothing. It returns:

- `diff`: the change in each file;
- `conflicts`: why the rename is unsafe, if it is. Examples are a name that the namespace already
  defines, or a file where `start` already means a local, another var or a `clojure.core` function;
- `mentions`: strings, keywords and quoted data that spell the old name. They stay unchanged.

When the preview has no conflict, `graphify.rename("my.ns/run", "start", apply=True)` writes the
files. Then run `graphify.build()` to update the graph. A file that changed after the preview stops
the write.

The rename uses the same symbol resolution as the graph. It follows `:as` aliases, `:refer`,
`:refer :all` and `:rename`, and skips parameters, `let` bindings and destructured names. It also
changes `declare`, `defmethod`, protocol implementations, `#'var`, `resolve`, syntax-quote and rich
`(comment ...)` forms. A record rename also changes its `->Rec` and `map->Rec` factories.
`graphify.references()` lists the same sites without a change.

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
