import io
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from graphify.kb import KnowledgeBase
from graphify.kb_server import KBServer

from vis_graphify import kb as kbc
from vis_graphify import presentation
from vis_graphify import tools as tools_module
from vis_graphify.graph import NodeNotFound
from vis_graphify.tools import GraphifyTools

TOKEN = "kb-token-for-the-tests-0123"


def _graph(place):
    """The graph.json of a project folder, or the graph file itself."""
    place = Path(place)
    return place if place.suffix == ".json" else place / "graphify-out" / "graph.json"


def _start(place, **options):
    """Serve a store in this process, as `graphify kb serve` does."""
    knowledge = KnowledgeBase(_graph(place), autoflush=False)
    options.setdefault("flush_interval", 0.05)
    server = KBServer(knowledge, "127.0.0.1", options.pop("port", 0), **options)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


def _stop(*servers):
    for server in servers:
        server.shutdown()
        server.server_close()


class FakeProcess:
    """A `graphify kb serve` child whose server runs in this process."""

    def __init__(self, server=None, code=None, pid=4242):
        self.server, self.code, self.pid, self.terminated = server, code, pid, False

    def poll(self):
        return self.code

    @property
    def returncode(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, -15
        if self.server:
            _stop(self.server)

    kill = terminate

    def wait(self, timeout=None):
        return self.code


class Spawner:
    """A spawn function: it serves the store of a `graphify kb serve` argv in this process."""

    def __init__(self, **options):
        self.calls, self.processes, self.options = [], [], options

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        port = int(argv[argv.index("--port") + 1])
        server = _start(argv[argv.index("serve") + 1], port=port, read_only="--read-only" in argv, **self.options)
        self.processes.append(FakeProcess(server))
        return self.processes[-1]

    def stop_all(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()


@pytest.fixture
def spawner():
    spawn = Spawner()
    yield spawn
    spawn.stop_all()


@pytest.fixture(autouse=True)
def no_configured_server(monkeypatch):
    for name in ("GRAPHIFY_KB_URL", "GRAPHIFY_KB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def served(project):
    server = _start(project)
    yield GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester"), server.url
    _stop(server)


@pytest.fixture
def hosted(project):
    """A server that needs a token, as on a host. Each test gets a new server and a new login backoff."""
    server = _start(project, token=TOKEN)
    yield server.url
    _stop(server)


def _other_project(project, tmp_path_factory):
    """Another project with a copy of the project graph."""
    other = tmp_path_factory.mktemp("other")
    (other / "graphify-out").mkdir()
    (other / "graphify-out" / "graph.json").write_text((project / "graphify-out" / "graph.json").read_text())
    return other


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- one server: reads, writes, undo ------------------------------------------


def test_search_and_explain_read_a_server(served):
    tools, url = served
    tools.write(
        [
            {"op": "add_node", "label": "Billing", "class": "concept"},
            {"op": "add_edge", "source": "kb:billing", "target": "src_app_db_query", "relation": "uses"},
        ],
        source=url,
    )
    found = tools.search("query", source=url)
    # The code node comes from the local graph, so the knowledge part leaves it out.
    assert [node.id for node in found.nodes] == ["src_app_db_query"] and found.source == url and found.note == ""
    assert "src_app_db_query" not in {hit.id for hit in found.knowledge}
    billing = tools.search("Billing", source=url)
    assert billing.nodes == () and [hit.id for hit in billing.knowledge] == ["kb:billing"]
    assert str(billing).startswith(f"1 knowledge matches in {url}:")
    assert presentation.search(billing).summary == "1 knowledge match for 'Billing'"
    # A code node gets its code edges and the knowledge edges that reach it.
    code = tools.explain("src_app_db_query", source=url)
    assert code.node.file and code.source == url and code.attributes == {}
    assert ("uses", "kb:billing", True) in {(e.relation, e.node.id, e.knowledge) for e in code.incoming}
    assert ("calls", "src_app_db_connect", False) in {(e.relation, e.node.id, e.knowledge) for e in code.outgoing}
    assert presentation.explain(code).summary.endswith(", 1 knowledge edge")
    # A knowledge node: by id or by exact label. Its code neighbour keeps its file and line.
    node = tools.explain("kb:billing", source=url)
    assert (node.node.label, node.node.kind, node.source) == ("Billing", "concept", url)
    (edge,) = node.outgoing
    assert (edge.relation, edge.node.id, edge.knowledge) == ("uses", "src_app_db_query", True) and edge.node.file
    assert tools.explain("Billing", source=url).node.id == "kb:billing"
    shown = presentation.explain(node)
    assert shown.headline == "Explain node" and shown.content[0].text == f"Billing (concept) [kb:billing] in {url}"
    # A class filter finds only knowledge nodes of that class and its subclasses.
    assert [hit.id for hit in tools.search("Billing", source=url, cls="concept").knowledge] == ["kb:billing"]
    assert tools.search("query", source=url, cls="concept").nodes == ()


def test_search_without_matches_is_empty(served):
    tools, url = served
    found = tools.search("zzz-nothing", source=url)
    assert (found.nodes, found.knowledge, found.source, found.note) == ((), (), url, "")
    assert presentation.search(found).summary == "no matches"
    with pytest.raises(NodeNotFound, match=f"No code node and nothing in {url} matches 'zzz-nothing'"):
        tools.explain("zzz-nothing", source=url)


def test_write_is_one_undoable_change(served):
    tools, url = served
    change = tools.write(
        [
            {"op": "add_node", "label": "Billing", "class": "concept"},
            {"op": "add_edge", "source": "kb:billing", "target": "src_app_db_query", "relation": "uses"},
            {"op": "add_document", "title": "Billing notes", "body": "[[Billing]] uses query()."},
        ],
        source=url,
    )
    assert change.summary == "Applied 1 add_node, 1 add_edge, 1 add_document"
    assert change.results[0]["id"] == "kb:billing" and change.version > 0 and len(change.seqs) == 1
    assert str(change).endswith(f"graphify.undo(source={url!r}) reverts it.")
    assert presentation.change("Write knowledge base")(change).summary == (
        f"Applied 1 add_node, 1 add_edge, 1 add_document in {url}, version {change.version}"
    )
    assert tools.explain("kb:billing", source=url).node.kind == "concept"
    assert "Billing notes" in {hit.label for hit in tools.search("Billing", source=url).knowledge}
    undone = tools.undo(source=url)
    assert undone.undone == change.seqs[0] and "Batch of 3" in undone.summary
    assert str(undone).startswith(f"Undid change {undone.undone}: Batch of 3")
    with pytest.raises(NodeNotFound):
        tools.explain("kb:billing", source=url)
    # The session has no other change there, so a second undo reverts nothing of another person.
    with pytest.raises(ValueError, match=f"This session has no change in {url} to revert. Give seq=N"):
        tools.undo(source=url)


def test_write_failure_applies_nothing(served):
    tools, url = served
    with pytest.raises(kbc.KBRequestError, match="unknown op 'nope'") as error:
        tools.write([{"op": "add_node", "label": "Kept out"}, {"op": "nope"}], source=url)
    assert error.value.status == 400
    assert tools.search("Kept out", source=url).knowledge == ()
    with pytest.raises(ValueError):
        tools.write([], source=url)


def test_ingest_text_file_and_inline_text(served, project):
    tools, url = served
    (project / "notes.md").write_text("# Database\nquery() calls connect() on start.\n")
    result = tools.ingest("notes.md", source=url, tags=["db"])
    assert result.document_id and result.mode == "text" and result.source == url
    assert "query()" in result.linked or "connect()" in result.linked
    inline = tools.ingest("inline.md", source=url, text="# Inline\nNo link here.", link=False)
    assert inline.title == "Inline" and inline.linked == ()
    assert presentation.ingest(result).headline == "Ingest document"
    # A document explains with its title, tags, text and links.
    document = tools.explain(result.document_id, source=url)
    assert (document.node.label, document.node.kind) == ("Database", "document")
    assert document.attributes["tags"] == ["db"] and "calls connect()" in document.attributes["body"]
    assert {edge.relation for edge in document.outgoing} == {"links"} and document.incoming == ()
    assert {edge.node.label for edge in document.outgoing} == set(result.linked)
    assert presentation.explain(document).content[2].text == "Text"
    # An ingest is an own change too: undo reverts the latest one first.
    tools.undo(source=url)
    assert {hit.label for hit in tools.search("Inline", source=url).knowledge} == set()
    with pytest.raises(FileNotFoundError):
        tools.ingest("missing.md", source=url)
    with pytest.raises(ValueError):
        tools.ingest("notes.md", source=url, mode="magic")


def test_validate_reports_findings(served):
    tools, url = served
    # graphify reports an orphan only for a node that the knowledge base added.
    tools.write([{"op": "add_node", "label": "Loose idea", "class": "concept"}], source=url)
    report = tools.validate(source=url, info=True)
    assert report.notes >= 1 and report.by_code.get("orphan_node", 0) >= 1
    assert presentation.validate(report).content[0].columns[0] == "Severity"


def test_read_only_server_refuses_writes(project):
    server = _start(project, read_only=True)
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project))
        with pytest.raises(kbc.KBRequestError, match="read-only") as error:
            tools.write([{"op": "add_node", "label": "X"}], source=server.url)
        assert error.value.status == 403
    finally:
        _stop(server)


def test_a_missing_loopback_server_points_to_the_local_store(project):
    tools = GraphifyTools(workspace_root=lambda: str(project))
    with pytest.raises(kbc.KBRequestError, match="Give source='local' for the store of this project"):
        tools.search("x", source="http://127.0.0.1:9")


# -- sources --------------------------------------------------------------------


def test_sources_resolve_local_stores_snapshots_and_servers(project, tmp_path_factory):
    tools = GraphifyTools(workspace_root=lambda: str(project))
    local = (project / "graphify-out" / "graph.json").resolve()
    for text in (None, "local", str(project), "graphify-out", "graphify-out/graph.json"):
        source = tools._kb_source(text)
        assert (source.name, source.kind, source.graph) == ("local", "local", local), text
    other = _other_project(project, tmp_path_factory)
    store = tools._kb_source(str(other))
    assert (store.kind, store.graph) == ("store", (other / "graphify-out" / "graph.json").resolve())
    assert tools._kb_source(store.name).key == store.key == tools._kb_source(str(store.graph)).key
    (other / "graphify-out" / "snapshot.json").write_text("{}")
    assert tools._kb_source(str(other)).kind == "snapshot"
    server = tools._kb_source("HTTPS://KB.example.com/team/")
    assert (server.name, server.kind, server.graph) == ("https://kb.example.com/team", "server", None)
    with pytest.raises(ValueError, match="ftp://kb.example.com is not an http or https URL"):
        tools._kb_source("ftp://kb.example.com")
    with pytest.raises(FileNotFoundError, match="nowhere is not a knowledge store"):
        tools._kb_source("nowhere")


def test_code_graph_skips_knowledge_nodes(project):
    graph_file = project / "graphify-out" / "graph.json"
    data = json.loads(graph_file.read_text())
    data["nodes"].append({"id": "kb:billing", "label": "Billing", "_origin": "kb", "class": "concept"})
    data["links"] += [
        {"source": "kb:billing", "target": "src_app_db_query", "relation": "uses", "_origin": "kb"},
        {"source": "src_app_db_connect", "target": "kb:billing", "relation": "documents"},
    ]
    graph_file.write_text(json.dumps(data))
    tools = GraphifyTools(workspace_root=lambda: str(project))
    # The code tools read only the tree-sitter graph: knowledge nodes and their edges stay out.
    status = tools.status()
    assert (status.nodes, status.edges) == (6, 4)
    assert tools.search("Billing").total == 0
    assert {edge.node.id for edge in tools.explain("src_app_db_connect").incoming} == {"src_app_db_query"}


# -- the store of the workspace: a managed server ---------------------------------


def test_local_store_starts_its_server_on_first_use_and_keeps_it(project, spawner, monkeypatch):
    # A loopback server needs no token, so the token of a hosted server stays out of its environment.
    monkeypatch.setenv("GRAPHIFY_KB_TOKEN", TOKEN)
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawner, kb_actor="tester")
    # A graph without knowledge answers search and explain alone: no server starts.
    assert tools.search("connect").source == "" and tools.explain("connect()").source == ""
    assert spawner.calls == []
    change = tools.write([{"op": "add_node", "label": "Billing", "class": "concept"}])
    ((argv, kwargs),) = spawner.calls
    graph = project.resolve() / "graphify-out" / "graph.json"
    assert argv[-7:-2] == ["kb", "serve", str(graph), "--host", "127.0.0.1"] and argv[-2] == "--port"
    assert kwargs["start_new_session"] and kwargs["cwd"] == str(project.resolve())
    assert "GRAPHIFY_KB_TOKEN" not in kwargs["env"]
    entry = json.loads((graph.parent / "kb-serve.json").read_text())
    assert entry == {"url": f"http://127.0.0.1:{argv[-1]}", "pid": 4242, "graph": str(graph), "read_only": False}
    # The running server answers at once, also before it saves the change.
    assert change.source == "local" and [hit.id for hit in tools.search("Billing").knowledge] == ["kb:billing"]
    assert tools.explain("kb:billing").node.kind == "concept"
    # After a gateway restart, a new instance finds the running server in kb-serve.json.
    later = GraphifyTools(workspace_root=lambda: str(project), spawn=lambda *a, **k: pytest.fail("no spawn"))
    assert later.explain("kb:billing").node.label == "Billing"
    shown = later.serve()
    assert (shown.started, shown.url, shown.pid, shown.source) == (False, entry["url"], 4242, "local")
    assert str(shown).startswith(f"Using knowledge base local at {entry['url']}: 7 nodes")
    assert presentation.serve(shown).headline == "Use knowledge base"
    killed = []

    def kill(pid, sig):
        killed.append((pid, sig))
        spawner.stop_all()

    monkeypatch.setattr(tools_module, "_kb_alive", lambda pid: pid == 4242 and not killed)
    monkeypatch.setattr(os, "kill", kill)
    stopped = later.serve(stop=True)
    assert killed == [(4242, signal.SIGTERM)] and stopped.stopped
    assert str(stopped).startswith(f"Stopped knowledge base local at {entry['url']}.")
    assert (presentation.serve(stopped).headline, presentation.serve(stopped).summary) == (
        "Stop knowledge base",
        f"stopped local at {entry['url']}",
    )
    assert not (graph.parent / "kb-serve.json").exists()
    with pytest.raises(RuntimeError, match="No server of this extension runs for local."):
        later.serve(stop=True)


def test_serve_gives_the_url_and_keeps_the_options_of_a_running_server(project, spawner):
    port = _free_port()
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawner)
    started = tools.serve(port=port)
    assert started.started and started.nodes == 6 and started.pid == 4242 and started.source == "local"
    assert started.url == f"http://127.0.0.1:{port}" and spawner.calls[0][0][-1] == str(port)
    assert started.log_path.endswith("kb-serve.log") and "admin UI" in str(started)
    assert presentation.serve(started).headline == "Start knowledge base"
    again = tools.serve()
    assert not again.started and again.url == started.url and len(spawner.calls) == 1
    for options, asked in (({"port": port + 1}, f"port {port + 1}"), ({"read_only": True}, "read-only mode")):
        with pytest.raises(RuntimeError, match=f"local has a server at {started.url} already. Stop it with") as error:
            tools.serve(**options)
        assert str(error.value).endswith(f"then start it with {asked}.")
    stopped = tools.serve(stop=True)
    assert stopped.stopped and spawner.processes[0].terminated and "Open the URL" not in str(stopped)
    assert tools.serve(read_only=True).read_only and spawner.calls[-1][0][-1] == "--read-only"


def test_another_store_answers_through_its_own_server(project, tmp_path_factory, spawner):
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawner)
    other = _other_project(project, tmp_path_factory)
    served = tools.serve(str(other))
    assert served.source != "local" and tools.search("connect", source=str(other)).source == served.source
    tools.serve(str(other), stop=True)
    # The next call starts the server of that store again.
    assert tools.search("connect", source=str(other)).source == served.source and len(spawner.calls) == 2


def test_serve_refuses_servers_and_addresses_of_other_machines(project):
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=lambda *a, **k: pytest.fail("no spawn"))
    with pytest.raises(ValueError, match="is a server already. serve starts the server of a store"):
        tools.serve("https://kb.example.com")
    with pytest.raises(ValueError, match="192.0.2.1 is not one of its addresses. Give a server elsewhere"):
        tools.serve(host="192.0.2.1")
    with pytest.raises(ValueError, match="Stop it where it runs"):
        tools.serve("https://kb.example.com", stop=True)


def test_serve_reports_a_crashed_server(project):
    def spawn(argv, stdout=None, **kwargs):
        stdout.write(b"error: address in use\n")
        return FakeProcess(code=1)

    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawn)
    with pytest.raises(RuntimeError, match="status 1") as error:
        tools.serve(port=_free_port())
    assert "address in use" in str(error.value)


def test_serve_refuses_a_port_with_another_graph(project, tmp_path_factory):
    server = _start(_other_project(project, tmp_path_factory))
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project), spawn=lambda *a, **k: pytest.fail("no spawn"))
        with pytest.raises(RuntimeError, match="already serves"):
            tools.serve(port=server.port)
    finally:
        _stop(server)


def test_a_stopped_store_server_restarts_for_a_read_but_not_for_a_write(project, spawner):
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawner)
    assert tools.search("connect", source="local").source == "local" and len(spawner.calls) == 1
    spawner.stop_all()
    # The read runs once more on a new server.
    assert tools.search("connect", source="local").source == "local" and len(spawner.calls) == 2
    spawner.stop_all()
    with pytest.raises(kbc.KBRequestError, match="stopped during POST /api/batch, so the change can be applied or not"):
        tools.write([{"op": "add_node", "label": "Maybe"}])
    assert len(spawner.calls) == 2
    assert tools.write([{"op": "add_node", "label": "Surely"}]).results and len(spawner.calls) == 3


def test_the_real_graphify_serves_the_store_and_stops_after_a_restart(project):
    tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester")
    started = tools.serve(timeout_s=60)
    try:
        assert started.started and started.nodes == 6
        tools.write([{"op": "add_node", "label": "Billing", "class": "concept"}])
        assert [hit.id for hit in tools.search("Billing").knowledge] == ["kb:billing"]
    finally:
        # A new instance, as after a gateway restart, stops the server through kb-serve.json.
        begun = time.monotonic()
        stopped = GraphifyTools(workspace_root=lambda: str(project)).serve(stop=True)
        (process,) = tools._kb_procs.values()
        process.wait(timeout=5)
    # The stop reaps its exited child at once instead of waiting for a zombie.
    assert stopped.stopped and stopped.pid == started.pid and time.monotonic() - begun < 10
    assert not tools_module._kb_alive(started.pid)
    # The server saved its change before it stopped.
    saved = "".join(path.read_text(errors="replace") for path in (project / "graphify-out").rglob("*.json"))
    assert "Billing" in saved and not (project / "graphify-out" / "kb-serve.json").exists()


# -- sessions -----------------------------------------------------------------------


def test_each_session_keeps_its_tokens_and_its_changes(hosted, project):
    session = {"id": "a"}
    asked = []

    def ask(url, check):
        asked.append(session["id"])
        return None if session["id"] == "c" else TOKEN

    tools = GraphifyTools(
        workspace_root=lambda: str(project), kb_actor="tester", kb_session=lambda: session["id"], kb_ask_token=ask
    )
    mine = tools.write([{"op": "add_node", "label": "From A", "class": "concept"}], source=hosted)
    session["id"] = "b"
    with pytest.raises(ValueError, match=f"This session has no change in {hosted} to revert"):
        tools.undo(source=hosted)
    # Session b asks for its own token.
    theirs = tools.write([{"op": "add_node", "label": "From B", "class": "concept"}], source=hosted)
    session["id"] = "a"
    # undo reverts the change of this session, not the later change of another session.
    undone = tools.undo(source=hosted)
    assert undone.undone == mine.seqs[0] != theirs.seqs[0] and asked == ["a", "b"]
    labels = {hit.label for hit in tools.search("From", source=hosted).knowledge}
    assert "From B" in labels and "From A" not in labels
    session["id"] = "c"
    # A session without a token gets none from another session.
    with pytest.raises(kbc.KBRequestError, match="needs a token") as error:
        tools.search("From", source=hosted)
    assert error.value.status == 401 and asked == ["a", "b", "c"]
    assert TOKEN not in repr(tools._kb_sessions) + str(error.value)


# -- tokens of a server elsewhere ----------------------------------------------------


def test_a_server_asks_for_its_token_once_in_each_session(hosted, project):
    without = GraphifyTools(workspace_root=lambda: str(project), kb_ask_token=lambda url, check: None)
    with pytest.raises(kbc.KBRequestError, match="needs a token. Save the server and its token in Settings") as error:
        without.search("query", source=hosted)
    assert error.value.status == 401
    asked = []

    def ask(url, check):
        asked.append(url)
        # The form shows the message of a refused token and closes on the right one.
        assert check("a-wrong-token-that-is-long") == "The server refused this token."
        assert check(TOKEN) is None
        return f" {TOKEN} "

    tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester", kb_ask_token=ask)
    tools.write([{"op": "add_node", "label": "Billing", "class": "concept"}], source=hosted + "/")
    assert tools.explain("kb:billing", source=hosted).node.label == "Billing"
    assert [hit.id for hit in tools.search("Billing", source=hosted).knowledge] == ["kb:billing"]
    tools.undo(source=hosted)
    with pytest.raises(NodeNotFound):
        tools.explain("kb:billing", source=hosted)
    assert asked == [hosted] and TOKEN not in repr(tools._kb_state())


def test_a_refused_token_of_the_session_asks_again_and_runs_the_call_once_more(hosted, project):
    asked = []

    def ask(url, check):
        asked.append(url)
        return TOKEN

    tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester", kb_ask_token=ask)
    assert tools.search("query", source=hosted).note == "" and asked == [hosted]
    # The server stops taking the token of this session, for example after a token change.
    tools._kb_state().tokens[hosted] = "an-old-token-that-is-long"
    change = tools.write([{"op": "add_node", "label": "Billing", "class": "concept"}], source=hosted)
    assert change.results and asked == [hosted, hosted]
    assert tools._kb_state().tokens[hosted] == TOKEN


def test_a_refused_token_stays_out_of_errors_and_notes(hosted, project, monkeypatch):
    wrong = "a-wrong-token-that-is-long"
    monkeypatch.setenv("GRAPHIFY_KB_URL", hosted)
    monkeypatch.setenv("GRAPHIFY_KB_TOKEN", wrong)
    tools = GraphifyTools(workspace_root=lambda: str(project))
    with pytest.raises(kbc.KBRequestError, match="refused the token in GRAPHIFY_KB_TOKEN") as error:
        tools.write([{"op": "add_node", "label": "X"}])
    assert error.value.status == 401 and wrong not in str(error.value)
    # Without a source, search and explain keep the code and tell why the knowledge is missing.
    found = tools.search("query")
    assert found.nodes and found.knowledge == () and found.source == hosted
    assert found.note.startswith(f"No knowledge from {hosted}: ") and wrong not in found.note
    explained = tools.explain("query()")
    assert explained.outgoing and explained.note.startswith(f"No knowledge from {hosted}: ")
    assert presentation.search(found).content[-1].text == found.note
    # A correct token passes also during the login backoff.
    monkeypatch.setenv("GRAPHIFY_KB_TOKEN", TOKEN)
    assert tools.search("query").note == ""


def test_environment_token_goes_only_to_the_configured_server(hosted, project, monkeypatch):
    other = _start(project, token=TOKEN)
    try:
        monkeypatch.setenv("GRAPHIFY_KB_URL", hosted + "/")
        monkeypatch.setenv("GRAPHIFY_KB_TOKEN", TOKEN)
        tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester")
        # The configured server and token serve the calls without a source.
        tools.write([{"op": "add_node", "label": "Billing", "class": "concept"}])
        found = tools.search("Billing")
        assert found.source == hosted and [hit.id for hit in found.knowledge] == ["kb:billing"]
        # A source that a call names never gets the token, also when its server would take it.
        with pytest.raises(
            kbc.KBRequestError, match=f"needs a token. The token in GRAPHIFY_KB_TOKEN goes only to {hosted}[.]"
        ) as error:
            tools.search("query", source=other.url)
        assert error.value.status == 401
        # An invalid configured server stops only the calls that use it.
        monkeypatch.setenv("GRAPHIFY_KB_URL", "ftp://kb.example.com")
        fresh = GraphifyTools(workspace_root=lambda: str(project))
        with pytest.raises(ValueError, match="GRAPHIFY_KB_URL: ftp://kb.example.com is not an http"):
            fresh.write([{"op": "add_node", "label": "X"}])
        found = fresh.search("query")
        assert found.nodes and found.note == (
            "No knowledge: GRAPHIFY_KB_URL: ftp://kb.example.com is not an http or https URL."
        )
    finally:
        _stop(other)


def test_settings_pair_their_server_and_token(hosted, project, monkeypatch):
    other = _start(project, token=TOKEN)
    saved = {"url": hosted + "/", "token": f" {TOKEN} "}
    try:
        # A project file can change the environment. Settings win, and their token never goes to another server.
        monkeypatch.setenv("GRAPHIFY_KB_URL", other.url)
        tools = GraphifyTools(workspace_root=lambda: str(project), kb_settings=lambda: (saved["url"], saved["token"]))
        found = tools.search("query")
        assert (found.source, found.note) == (hosted, "")
        with pytest.raises(kbc.KBRequestError, match=f"The token in Settings goes only to {hosted}[.]"):
            tools.search("query", source=other.url)
        # Without a server in Settings, their token stays unused, also for the server in the environment.
        saved["url"] = ""
        fresh = GraphifyTools(workspace_root=lambda: str(project), kb_settings=lambda: (saved["url"], saved["token"]))
        with pytest.raises(kbc.KBRequestError, match="needs a token[.] Save the server and its token") as error:
            fresh.write([{"op": "add_node", "label": "X"}])
        assert error.value.status == 401 and TOKEN not in str(error.value)
        saved["url"] = "ftp://kb.example.com"
        with pytest.raises(ValueError, match="graphify_kb_url: ftp://kb.example.com is not an http"):
            fresh.write([{"op": "add_node", "label": "X"}])
    finally:
        _stop(other)


def _redirect_server(target):
    """A server that answers every request with a 308 to `target` plus the request path."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Moved(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(308)
            self.send_header("Location", target + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Moved)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_a_server_follows_a_redirect_only_on_the_same_host(hosted, project):
    port = hosted.rsplit(":", 1)[1]
    same, same_url = _redirect_server(hosted)
    other, other_url = _redirect_server(f"http://localhost:{port}")
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project), kb_ask_token=lambda url, check: TOKEN)
        assert tools.search("query", source=same_url).source == same_url
        assert tools._kb_state().servers == {same_url: hosted} and set(tools._kb_state().tokens) == {hosted}
        with pytest.raises(kbc.KBRequestError, match=f"redirects to http://localhost:{port}/api/health") as error:
            tools.search("query", source=other_url)
        assert error.value.status == 308
        # A write never follows a redirect: urllib would send the token on and turn the POST into a GET.
        with pytest.raises(kbc.KBRequestError, match="redirects to"):
            kbc.KBClient(url=same_url, token=TOKEN).request("POST", "/api/batch", {"ops": []})
    finally:
        _stop(same, other)


def test_moved_base_keeps_the_host_and_https():
    from vis_graphify.tools import _moved_base

    def moved(base, location, status=301):
        return _moved_base(base, kbc.KBRequestError("moved", status, location))

    assert moved("http://kb.example.com", "https://kb.example.com/api/health") == "https://kb.example.com"
    assert moved("http://kb.example.com/team", "https://kb.example.com/team/api/health", 308) == (
        "https://kb.example.com/team"
    )
    assert moved("https://kb.example.com", "http://kb.example.com/api/health") is None
    assert moved("http://kb.example.com", "https://other.example.net/api/health") is None
    assert moved("http://kb.example.com", "https://kb.example.com/login") is None
    assert moved("http://kb.example.com", "", 404) is None


def test_normalize_url():
    assert kbc.normalize_url("kb.example.com/") == "https://kb.example.com"
    assert kbc.normalize_url(" HTTPS://kb.example.com/team/?q=1#top ") == "https://kb.example.com/team"
    assert kbc.normalize_url("127.0.0.1:7878") == "http://127.0.0.1:7878"
    assert kbc.normalize_url("localhost:7878") == "http://localhost:7878"
    assert kbc.normalize_url("[::1]:7878") == "http://[::1]:7878"
    assert kbc.normalize_url("https://KB.Example.com/Team/") == "https://kb.example.com/Team"
    for bad in ("", "ftp://kb.example.com", "https://"):
        with pytest.raises(ValueError):
            kbc.normalize_url(bad)
    with pytest.raises(ValueError, match="Do not put the token in the URL") as error:
        kbc.normalize_url("https://me:hunter2-hunter2@kb.example.com")
    assert "hunter2" not in str(error.value)
    assert kbc.is_loopback_url("http://127.0.0.2:1") and not kbc.is_loopback_url("https://kb.example.com")


def test_server_text_warns_about_plain_http_off_loopback():
    def text(url):
        return str(kbc.KBServer(url, "/p/graph.json", 3, 2, 1, 1, False, False))

    assert "unencrypted" in text("http://10.0.0.5:7878")
    assert "unencrypted" not in text("https://kb.example.com") + text("http://127.0.0.1:7878")


class _Answer(io.BytesIO):
    """An HTTP answer for a KBClient opener."""

    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


def test_client_keeps_the_base_path_and_explains_hosted_errors():
    import gzip
    import urllib.error

    sent = []

    def answer(request, timeout):
        sent.append((request.full_url, request.get_header("Authorization"), request.get_header("Accept-encoding")))
        body = gzip.compress(json.dumps({"ok": True}).encode())
        return _Answer(body, {"Content-Encoding": "gzip", kbc.CHANGE_HEADER: "12,13"})

    client = kbc.KBClient(url="https://kb.example.com/team", token=TOKEN, _opener=answer)
    assert client.request("POST", "/api/batch", {"ops": []}) == {"ok": True} and client.changes == (12, 13)
    assert sent == [("https://kb.example.com/team/api/batch", f"Bearer {TOKEN}", "gzip")]
    assert TOKEN not in repr(client)

    def refuse(code, body, headers=None):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "refused", headers or {}, io.BytesIO(body))

        return kbc.KBClient(url="https://kb.example.com", _opener=opener)

    failed = refuse(403, b'{"error": "unknown Host header"}')
    with pytest.raises(kbc.KBRequestError, match="--allow-host kb.example.com"):
        failed.request("GET", "/api/stats")
    assert failed.changes == ()
    with pytest.raises(kbc.KBRequestError, match="wait and try again [(]after 3 s[)]"):
        refuse(429, b'{"error": "too many failed logins; wait and try again"}', {"Retry-After": "3"}).request(
            "GET", "/api/stats"
        )
    with pytest.raises(kbc.KBRequestError, match="broken gzip"):
        kbc.KBClient(
            url="https://kb.example.com", _opener=lambda request, timeout: _Answer(b"no", {"Content-Encoding": "gzip"})
        ).request("GET", "/api/stats")

    def down(request, timeout):
        raise urllib.error.URLError("no such host")

    with pytest.raises(kbc.KBRequestError, match="Check the URL") as error:
        kbc.KBClient(url="https://kb.example.com", _opener=down).request("GET", "/api/health")
    assert "graphify.serve" not in str(error.value)


# -- snapshots and pages ---------------------------------------------------------------


def test_snapshot_copies_a_server_into_a_read_only_local_source(project, tmp_path_factory, spawner, monkeypatch):
    remote = _start(_other_project(project, tmp_path_factory), token=TOKEN)
    # A copy keeps SQLite, whatever backend the environment names for new stores.
    monkeypatch.setenv("GRAPHIFY_KB_STORE", "memory")
    try:
        tools = GraphifyTools(
            workspace_root=lambda: str(project), spawn=spawner, kb_actor="tester", kb_ask_token=lambda url, check: TOKEN
        )
        tools.write(
            [
                {"op": "add_node", "label": "Billing", "class": "concept"},
                {"op": "add_document", "title": "Billing notes", "body": "[[Billing]] uses query()."},
            ],
            source=remote.url,
        )
        snap = tools.snapshot(remote.url)
        name = f"graphify-out/snapshots/127.0.0.1-{remote.port}"
        # The copy has the 6 code nodes, the concept and the node of the document.
        assert (snap.source, snap.name, snap.nodes, snap.documents, snap.assets) == (remote.url, name, 8, 1, 0)
        assert snap.version > 0 and Path(snap.path) == (project / name).resolve()
        assert (project / name / "kb" / "store.sqlite").is_file()
        assert str(snap).endswith(f"Read it with source={name!r}. The copy is read-only.")
        assert presentation.snapshot(snap).summary.startswith(f"{remote.url} to {name}: 8 nodes,")
        meta = json.loads((project / name / "snapshot.json").read_text())
        assert (meta["source"], meta["version"], meta["assets"]) == (remote.url, snap.version, False)
        assert not any(TOKEN in f.read_text(errors="replace") for f in (project / name).rglob("*") if f.is_file())
        # The copy answers without the remote server, through a read-only server of this machine.
        assert "kb:billing" in {hit.id for hit in tools.search("Billing", source=name).knowledge}
        assert spawner.calls[-1][0][-1] == "--read-only"
        assert tools.explain("kb:billing", source=name).node.kind == "concept"
        with pytest.raises(ValueError, match="is a read-only snapshot. Write to the source that it copies"):
            tools.write([{"op": "add_node", "label": "X"}], source=name)
        with pytest.raises(ValueError, match="is a snapshot. Copy the source that it copies"):
            tools.snapshot(name)
        # A new snapshot replaces the copy, and the running server of the copy reads the new files.
        tools.write([{"op": "add_node", "label": "Invoices", "class": "concept"}], source=remote.url)
        again = tools.snapshot(remote.url)
        assert again.name == name and again.version > snap.version and again.nodes == 9
        assert tools.search("Invoices", source=name).knowledge and len(spawner.calls) == 1
        assert not [p for p in (project / "graphify-out" / "snapshots").iterdir() if p.name.startswith(".")]
        # The remote server can go away: the copy still answers.
        _stop(remote)
        assert tools.search("Invoices", source=name).source == name
        assert tools.serve(name, stop=True).stopped
    finally:
        try:
            _stop(remote)
        except OSError:
            pass


def test_the_store_backend_in_settings_reaches_the_kb_children(project, spawner, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_KB_STORE", "memory")
    runs = []

    def run(argv, **kwargs):
        runs.append((argv, kwargs))
        Path(argv[argv.index("--out") + 1]).write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    saved = {"store": " sqlite "}
    tools = GraphifyTools(
        workspace_root=lambda: str(project), spawn=spawner, runner=run, kb_store=lambda: saved["store"]
    )
    tools.write([{"op": "add_node", "label": "Billing", "class": "concept"}])
    # The backend in Settings wins over the environment, for the server and for the page export.
    assert spawner.calls[-1][1]["env"]["GRAPHIFY_KB_STORE"] == "sqlite"
    tools.publish()
    ((argv, options),) = runs
    assert argv[argv.index("kb") + 1] == "export" and options["env"]["GRAPHIFY_KB_STORE"] == "sqlite"
    # Each child runs in the project, so that a `module:attr` backend of the project imports.
    assert Path(spawner.calls[-1][1]["cwd"]).resolve() == Path(options["cwd"]).resolve() == project.resolve()
    # Without a backend in Settings, the environment names it.
    saved["store"] = ""
    assert tools._kb_env()["GRAPHIFY_KB_STORE"] == "memory"
    assert GraphifyTools(workspace_root=lambda: str(project))._kb_env()["GRAPHIFY_KB_STORE"] == "memory"


def test_snapshot_export_refuses_unsafe_files(tmp_path):
    from vis_graphify.tools import _kb_write_export

    def export(*files):
        return {"format": "graphify-kb-export", "files": list(files)}

    graph = {"path": "graph.json", "json": {"nodes": [{"id": "a"}], "links": []}}
    with pytest.raises(kbc.KBRequestError, match="sent no store export"):
        _kb_write_export(tmp_path / "a", {"files": []})
    for bad in ("../escape.json", "/etc/x", "kb\\x", "c:x", "", "kb/.lock"):
        with pytest.raises(kbc.KBRequestError, match="unsafe path"):
            _kb_write_export(tmp_path / "b", export(graph, {"path": bad, "text": "x"}))
    # Only the files of a store export go in: no store file, script or other folder.
    for other in ("kb/store.sqlite", "kb/store.json", "run.sh", "kb/documents/d1/a.png"):
        with pytest.raises(kbc.KBRequestError, match="does not hold"):
            _kb_write_export(tmp_path / "b", export(graph, {"path": other, "text": "x"}))
    assert not (tmp_path / "b").exists()
    with pytest.raises(kbc.KBRequestError, match="has no graph.json"):
        _kb_write_export(tmp_path / "c", export({"path": "kb/overlay.json", "json": {}}))
    meta = {"path": "kb/documents/d1/meta.json", "json": {"title": "D"}}
    asset = {"path": "kb/documents/d1/assets/a.png", "base64": "aGk="}
    overlay = {"path": "kb/overlay.json", "json": {"version": 1, "nodes": {"kb:n": {"id": "kb:n", "label": "N"}}}}
    counts = _kb_write_export(tmp_path / "d", export(graph, meta, asset, overlay))
    assert counts == {"nodes": 1, "edges": 0, "documents": 1, "assets": 1}
    assert (tmp_path / "d" / "kb" / "documents" / "d1" / "assets" / "a.png").read_bytes() == b"hi"
    # The edits go straight into the store of the copy.
    assert (tmp_path / "d" / "kb" / "store.sqlite").is_file() and not (tmp_path / "d" / "kb" / "overlay.json").exists()


def test_publish_writes_the_page_of_the_local_store_with_its_unsaved_changes(project):
    # The server keeps changes in memory for an hour, so only the flush of publish saves them.
    spawn = Spawner(flush_interval=3600)
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawn)
        tools.write([{"op": "add_node", "label": "Unsaved idea", "class": "concept"}])
        published = tools.publish()
        assert Path(published.path) == (project / "graphify-out" / "kb.html").resolve() and published.bytes > 1000
        assert published.source == "local" and "Unsaved idea" in Path(published.path).read_text()
        with pytest.raises(ValueError, match="path must be an .html file name without folders"):
            tools.publish(path="../escape.html")
    finally:
        spawn.stop_all()


def test_publish_copies_a_server_first(served, project):
    tools, url = served
    tools.write([{"op": "add_node", "label": "Remote idea", "class": "concept"}], source=url)
    published = tools.publish(source=url)
    page = project / "graphify-out" / f"kb-127.0.0.1-{url.rsplit(':', 1)[1]}.html"
    assert Path(published.path) == page.resolve() and published.source == url and published.bytes > 1000
    assert "Remote idea" in page.read_text()


# -- moved code nodes ----------------------------------------------------------------------


def test_moved_nodes_reach_the_configured_server(project, tmp_path_factory, monkeypatch):
    server = _start(_other_project(project, tmp_path_factory))
    try:
        monkeypatch.setenv("GRAPHIFY_KB_URL", server.url)
        tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester")
        # Its graph still has the old node, so the move waits there. A node without records changes nothing.
        lines = tools._kb_moves(project.resolve(), {"src_app_db_query": "src_app_db_fetch", "gone": "came"})
        assert lines == (f"{server.url}: 0 moved, 1 pending, 1 with no records at the old id",)
    finally:
        _stop(server)


def _with_records(place):
    knowledge = KnowledgeBase(_graph(place))
    note = {"op": "add_document", "title": "Query notes", "body": "Slow.", "nodes": ["src_app_db_query"]}
    knowledge.batch([note], actor="tester")
    knowledge.flush()


def test_moved_nodes_reach_the_project_store(project):
    _with_records(project)
    runs = []

    def run(argv, **kwargs):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, 0, '[{"status": "moved"}]', "")

    tools = GraphifyTools(
        workspace_root=lambda: str(project), runner=run, spawn=lambda *a, **k: pytest.fail("no spawn")
    )
    assert tools._kb_moves(project.resolve(), {"src_app_db_query": "src_app_db_fetch"}) == (
        "graphify-out/kb: 1 moved, 0 pending, 0 with no records at the old id",
    )
    assert [argv[argv.index("--graph") + 1] for argv in runs] == [
        str((project / "graphify-out" / "graph.json").resolve())
    ]


def test_moved_nodes_skip_a_server_of_the_project_store(project, monkeypatch):
    _with_records(project)
    local = _start(project)
    runs = []

    def run(argv, **kwargs):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, 0, '[{"status": "moved"}]', "")

    try:
        monkeypatch.setenv("GRAPHIFY_KB_URL", local.url)
        tools = GraphifyTools(workspace_root=lambda: str(project), runner=run)
        # The configured server serves this store, so only `graphify kb move` writes the moves.
        lines = tools._kb_moves(project.resolve(), {"src_app_db_query": "src_app_db_fetch"})
        assert lines == ("graphify-out/kb: 1 moved, 0 pending, 0 with no records at the old id",) and len(runs) == 1
    finally:
        _stop(local)


def test_moved_nodes_report_a_configured_server_that_fails(project, monkeypatch):
    tools = GraphifyTools(workspace_root=lambda: str(project))
    moved = {"src_app_db_query": "src_app_db_fetch"}
    dead = f"http://127.0.0.1:{_free_port()}"
    monkeypatch.setenv("GRAPHIFY_KB_URL", dead)
    (line,) = tools._kb_moves(project.resolve(), moved)
    assert line.startswith(f"{dead}: not updated: No knowledge base server answers at {dead}")
    monkeypatch.setenv("GRAPHIFY_KB_URL", "ftp://kb.example.com")
    assert tools._kb_moves(project.resolve(), moved) == (
        "configured server: not updated: GRAPHIFY_KB_URL: ftp://kb.example.com is not an http or https URL.",
    )


# -- knowledge in search and explain -----------------------------------------------------


def test_search_and_explain_read_the_knowledge_of_the_graph(project, spawner):
    _with_records(project)
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawner)
    # The store of the graph holds a document, so search and explain start its server without a source.
    found = tools.search("Query notes")
    assert found.source == "local" and len(spawner.calls) == 1
    (document,) = [hit for hit in found.knowledge if hit.kind == "document"]
    assert document.label == "Query notes"
    shown = presentation.search(found)
    assert shown.content[-1].columns == ("Label", "Kind", "Class", "Source")
    code = tools.explain("src_app_db_query")
    assert code.source == "local" and any(edge.knowledge for edge in code.incoming + code.outgoing)
    # A document id explains the document: its text and the nodes that it links.
    explained = tools.explain(document.id)
    assert (explained.node.label, explained.node.kind, explained.source) == ("Query notes", "document", "local")
    assert explained.attributes["body"] == "Slow." and "body_chars" not in explained.attributes
    assert [(edge.relation, edge.node.id) for edge in explained.outgoing] == [("links", "src_app_db_query")]
    assert explained.outgoing[0].node.file and explained.outgoing[0].knowledge
    # A name that is neither code nor knowledge stays an error that points to search.
    with pytest.raises(NodeNotFound, match="No code node and nothing in local matches 'nowhere'. Try graphify.search"):
        tools.explain("nowhere")
    assert len(spawner.calls) == 1


def test_a_long_document_text_ends_at_the_limit(project, monkeypatch):
    from vis_graphify import graph as graph_module
    from vis_graphify.tools import KB_BODY_CHARS, _document_explanation

    graph = graph_module.load(project)
    answer = {"id": "d1", "title": "Long", "body": "x" * (KB_BODY_CHARS + 5), "linked": [{"id": "kb:a", "label": "A"}]}
    explained = _document_explanation(graph, answer, "local")
    assert (
        len(explained.attributes["body"]) == KB_BODY_CHARS and explained.attributes["body_chars"] == KB_BODY_CHARS + 5
    )
    assert (explained.outgoing[0].node.label, explained.outgoing[0].node.kind) == ("A", "knowledge")
    assert presentation.explain(explained).content[-1].rows == (("links", "A", "knowledge"),)


def test_a_graph_without_knowledge_starts_no_server_and_a_dead_server_gives_a_note(project, monkeypatch):
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=lambda *a, **k: pytest.fail("no spawn"))
    found = tools.search("query")
    assert (found.source, found.note, found.knowledge) == ("", "", ()) and found.nodes
    with pytest.raises(NodeNotFound) as error:
        tools.explain("nowhere")
    assert str(error.value) == "No node matches 'nowhere'. Try graphify.search()."
    dead = f"http://127.0.0.1:{_free_port()}"
    monkeypatch.setenv("GRAPHIFY_KB_URL", dead)
    found = tools.search("query")
    assert found.nodes and found.source == dead and found.note.startswith(f"No knowledge from {dead}: ")
    assert tools.explain("query()").note.startswith(f"No knowledge from {dead}: ")
    with pytest.raises(NodeNotFound, match=f"No code node matches 'nowhere', and {dead} did not answer"):
        tools.explain("nowhere")
    # A source that a call names raises its failure.
    with pytest.raises(kbc.KBRequestError, match="No knowledge base server answers"):
        tools.search("query", source=dead)
