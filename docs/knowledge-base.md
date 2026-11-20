# Knowledge base guide

## When to use

Use this guide to [record knowledge](#knowledge-base) with your code, [connect to a team server](#use-a-hosted-knowledge-base),
or [make a local copy](#copy-a-knowledge-source). To inspect or change the code itself, see the [code graph reference](code-graph.md).

## Knowledge base

The knowledge base adds your concepts, documents and edges to the code graph, and checks them
against an ontology. You and the agent edit the same store, and each change is undoable.
`graphify.search` and `graphify.explain` show this knowledge next to the code. The code itself
always comes from the local tree-sitter graph, also in `affected`, `rename` and `move`.

A knowledge source is one of these:

- `local`: the store of the project in `<root>/.graphify/kb/`. This is the default.
- The folder or `graph.json` of another store on this machine, for example `../team-notes`.
- A snapshot folder in `.graphify/snapshots/`. See [Copy a knowledge source](#copy-a-knowledge-source).
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
print(graphify.publish())                       # static .graphify/kb.html
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
- The project store `.graphify/kb/` gets the new ids through `graphify kb move`.
- The [configured server](#save-the-server-in-settings) gets them too, over HTTP, unless it serves
  the project store. A snapshot gets nothing: the next `snapshot` copies the moved records.
- `kb` has one line for each knowledge base, for example
  `.graphify/kb: 1 moved, 0 pending, 0 with no records at the old id`.

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
  copies the store of the server, then writes `.graphify/kb-<host>-<port>.html` on your machine.

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
graphify kb serve .graphify/graph.json --allow-host kb.example.com
```

- The server listens on `127.0.0.1:7878`. Point the proxy for `kb.example.com` at that address.
- With a token, every API call except `/api/health` needs the token.
- `--allow-host` names the public hostname. The server refuses requests for other host names.
- The server reads the token from `GRAPHIFY_KB_TOKEN`, so the token does not show in the process list.
- The stores of this extension always use SQLite. To use another store backend on a server that you
  host, start it with `graphify kb serve --store NAME`. See
  [Use another store](https://github.com/Blockether/graphify/blob/v8/docs/kb.md#use-another-store).

### Copy a knowledge source

`graphify.snapshot(source)` copies a source into `.graphify/snapshots/<name>/` of the project:
the graph, the ontology, the knowledge records and the documents. Read the copy without the server,
for example offline or to compare it with the server later.

```python
snap = graphify.snapshot("https://kb.example.com")  # .graphify/snapshots/kb.example.com
print(graphify.search("billing", source=snap.name))
```

- A snapshot is read-only. Write to the source that it copies, then copy that source again.
- A new snapshot with the same name replaces the old copy. Give `name=` to keep more copies.
- Give `assets=True` to also copy the files of the documents, such as images and PDFs.
- The copy holds no token. The server needs graphify v0.9.93-blockether.17 or later.
