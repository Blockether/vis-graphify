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


def test_search_and_node_read_a_server(served):
    tools, url = served
    found = tools.kb_search("query", source=url)
    assert found.hits and found.hits[0].id == "src_app_db_query"
    assert found.source == url and str(found).startswith(f"1 match for 'query' in {url}:")
    assert presentation.kb_search(found).summary == f"1 match for 'query' in {url}"
    node = tools.kb_node("src_app_db_query", source=url)
    # Since graphify v0.9.93-blockether.12, an extracted function has the class code.function.
    assert (node.label, node.cls, node.kb) == ("query()", "code.function", False)
    assert {(e.direction, e.relation, e.other) for e in node.edges} >= {
        ("out", "calls", "src_app_db_connect"),
        ("in", "calls", "src_app_core_run"),
    }
    assert tools.kb_node("connect()", source=url).id == "src_app_db_connect"
    assert presentation.kb_node(node).summary.startswith("query() (code.function)")
    # A class filter also finds the subclasses: code finds the code.function node.
    assert tools.kb_search("query", source=url, cls="code").hits[0].id == "src_app_db_query"


def test_search_without_matches_is_empty(served):
    tools, url = served
    found = tools.kb_search("zzz-nothing", source=url)
    assert found.hits == () and str(found) == f"Nothing in {url} matches 'zzz-nothing'."
    assert presentation.kb_search(found).summary == "no matches"


def test_write_is_one_undoable_change(served):
    tools, url = served
    change = tools.kb_write(
        [
            {"op": "add_node", "label": "Billing", "class": "concept"},
            {"op": "add_edge", "source": "kb:billing", "target": "src_app_db_query", "relation": "uses"},
            {"op": "add_document", "title": "Billing notes", "body": "[[Billing]] uses query()."},
        ],
        source=url,
    )
    assert change.summary == "Applied 1 add_node, 1 add_edge, 1 add_document"
    assert change.results[0]["id"] == "kb:billing" and change.version > 0 and len(change.seqs) == 1
    assert str(change).endswith(f"graphify.kb_undo(source={url!r}) reverts it.")
    assert presentation.kb_change("Write knowledge base")(change).summary == (
        f"Applied 1 add_node, 1 add_edge, 1 add_document in {url}, version {change.version}"
    )
    node = tools.kb_node("kb:billing", source=url)
    assert node.kb and node.cls == "concept"
    assert "Billing notes" in {hit.label for hit in tools.kb_search("Billing", source=url).hits}
    undone = tools.kb_undo(source=url)
    assert undone.undone == change.seqs[0] and "Batch of 3" in undone.summary
    assert str(undone).startswith(f"Undid change {undone.undone}: Batch of 3")
    with pytest.raises(kbc.KBRequestError) as error:
        tools.kb_node("kb:billing", source=url)
    assert error.value.status == 404
    # The session has no other change there, so a second undo reverts nothing of another person.
    with pytest.raises(ValueError, match=f"This session has no change in {url} to revert. Give seq=N"):
        tools.kb_undo(source=url)


def test_write_failure_applies_nothing(served):
    tools, url = served
    with pytest.raises(kbc.KBRequestError, match="unknown op 'nope'") as error:
        tools.kb_write([{"op": "add_node", "label": "Kept out"}, {"op": "nope"}], source=url)
    assert error.value.status == 400
    assert tools.kb_search("Kept out", source=url).hits == ()
    with pytest.raises(ValueError):
        tools.kb_write([], source=url)


def test_ingest_text_file_and_inline_text(served, project):
    tools, url = served
    (project / "notes.md").write_text("# Database\nquery() calls connect() on start.\n")
    result = tools.kb_ingest("notes.md", source=url, tags=["db"])
    assert result.document_id and result.mode == "text" and result.source == url
    assert "query()" in result.linked or "connect()" in result.linked
    inline = tools.kb_ingest("inline.md", source=url, text="# Inline\nNo link here.", link=False)
    assert inline.title == "Inline" and inline.linked == ()
    assert presentation.kb_ingest(result).headline == "Ingest document"
    # An ingest is an own change too: kb_undo reverts the latest one first.
    tools.kb_undo(source=url)
    assert {hit.label for hit in tools.kb_search("Inline", source=url).hits} == set()
    with pytest.raises(FileNotFoundError):
        tools.kb_ingest("missing.md", source=url)
    with pytest.raises(ValueError):
        tools.kb_ingest("notes.md", source=url, mode="magic")


def test_validate_reports_findings(served):
    tools, url = served
    # graphify reports an orphan only for a node that the knowledge base added.
    tools.kb_write([{"op": "add_node", "label": "Loose idea", "class": "concept"}], source=url)
    report = tools.kb_validate(source=url, info=True)
    assert report.notes >= 1 and report.by_code.get("orphan_node", 0) >= 1
    assert presentation.kb_validate(report).content[0].columns[0] == "Severity"


def test_read_only_server_refuses_writes(project):
    server = _start(project, read_only=True)
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project))
        with pytest.raises(kbc.KBRequestError, match="read-only") as error:
            tools.kb_write([{"op": "add_node", "label": "X"}], source=server.url)
        assert error.value.status == 403
    finally:
        _stop(server)


def test_a_missing_loopback_server_points_to_the_local_store(project):
    tools = GraphifyTools(workspace_root=lambda: str(project))
    with pytest.raises(kbc.KBRequestError, match="Give source='local' for the store of this project"):
        tools.kb_search("x", source="http://127.0.0.1:9")


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
    assert tools.kb_search("connect").source == "local"
    ((argv, kwargs),) = spawner.calls
    graph = project.resolve() / "graphify-out" / "graph.json"
    assert argv[-7:-2] == ["kb", "serve", str(graph), "--host", "127.0.0.1"] and argv[-2] == "--port"
    assert kwargs["start_new_session"] and kwargs["cwd"] == str(project.resolve())
    assert "GRAPHIFY_KB_TOKEN" not in kwargs["env"]
    entry = json.loads((graph.parent / "kb-serve.json").read_text())
    assert entry == {"url": f"http://127.0.0.1:{argv[-1]}", "pid": 4242, "graph": str(graph), "read_only": False}
    change = tools.kb_write([{"op": "add_node", "label": "Billing", "class": "concept"}])
    assert change.source == "local" and tools.kb_node("kb:billing").kb
    # After a gateway restart, a new instance finds the running server in kb-serve.json.
    later = GraphifyTools(workspace_root=lambda: str(project), spawn=lambda *a, **k: pytest.fail("no spawn"))
    assert later.kb_node("kb:billing").label == "Billing"
    shown = later.kb_serve()
    assert (shown.started, shown.url, shown.pid, shown.source) == (False, entry["url"], 4242, "local")
    assert str(shown).startswith(f"Using knowledge base local at {entry['url']}: 7 nodes")
    assert presentation.kb_server(shown).headline == "Use knowledge base"
    killed = []

    def kill(pid, sig):
        killed.append((pid, sig))
        spawner.stop_all()

    monkeypatch.setattr(tools_module, "_kb_alive", lambda pid: pid == 4242 and not killed)
    monkeypatch.setattr(os, "kill", kill)
    stopped = later.kb_stop()
    assert killed == [(4242, signal.SIGTERM)] and stopped.stopped
    assert str(stopped).startswith(f"Stopped knowledge base local at {entry['url']}.")
    assert presentation.kb_stopped(stopped).summary == f"stopped local at {entry['url']}"
    assert not (graph.parent / "kb-serve.json").exists()
    with pytest.raises(RuntimeError, match="No server of this extension runs for local."):
        later.kb_stop()


def test_serve_gives_the_url_and_keeps_the_options_of_a_running_server(project, spawner):
    port = _free_port()
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawner)
    started = tools.kb_serve(port=port)
    assert started.started and started.nodes == 6 and started.pid == 4242 and started.source == "local"
    assert started.url == f"http://127.0.0.1:{port}" and spawner.calls[0][0][-1] == str(port)
    assert started.log_path.endswith("kb-serve.log") and "admin UI" in str(started)
    assert presentation.kb_server(started).headline == "Start knowledge base"
    again = tools.kb_serve()
    assert not again.started and again.url == started.url and len(spawner.calls) == 1
    for options, asked in (({"port": port + 1}, f"port {port + 1}"), ({"read_only": True}, "read-only mode")):
        with pytest.raises(RuntimeError, match=f"local has a server at {started.url} already. Stop it with") as error:
            tools.kb_serve(**options)
        assert str(error.value).endswith(f"then start it with {asked}.")
    stopped = tools.kb_stop()
    assert stopped.stopped and spawner.processes[0].terminated and "Open the URL" not in str(stopped)
    assert tools.kb_serve(read_only=True).read_only and spawner.calls[-1][0][-1] == "--read-only"


def test_use_of_the_url_of_a_store_server_keeps_the_store(project, tmp_path_factory, spawner):
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawner)
    other = _other_project(project, tmp_path_factory)
    served = tools.kb_serve(str(other))
    used = tools.kb_use(served.url)
    # That server gets a new port at its next start, so the session keeps the store and not the URL.
    assert used.source == served.source != served.url and used.pid == 4242
    assert tools.kb_sources().current == served.source
    tools.kb_stop(str(other))
    assert tools.kb_search("connect").source == served.source and len(spawner.calls) == 2


def test_serve_refuses_servers_and_addresses_of_other_machines(project):
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=lambda *a, **k: pytest.fail("no spawn"))
    with pytest.raises(ValueError, match="is a server already. kb_serve starts the server of a store"):
        tools.kb_serve("https://kb.example.com")
    with pytest.raises(ValueError, match="192.0.2.1 is not one of its addresses. Use kb_use"):
        tools.kb_serve(host="192.0.2.1")
    with pytest.raises(ValueError, match="Stop it where it runs"):
        tools.kb_stop("https://kb.example.com")


def test_serve_reports_a_crashed_server(project):
    def spawn(argv, stdout=None, **kwargs):
        stdout.write(b"error: address in use\n")
        return FakeProcess(code=1)

    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawn)
    with pytest.raises(RuntimeError, match="status 1") as error:
        tools.kb_serve(port=_free_port())
    assert "address in use" in str(error.value)


def test_serve_refuses_a_port_with_another_graph(project, tmp_path_factory):
    server = _start(_other_project(project, tmp_path_factory))
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project), spawn=lambda *a, **k: pytest.fail("no spawn"))
        with pytest.raises(RuntimeError, match="already serves"):
            tools.kb_serve(port=server.port)
    finally:
        _stop(server)


def test_a_stopped_store_server_restarts_for_a_read_but_not_for_a_write(project, spawner):
    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawner)
    assert tools.kb_search("connect").hits and len(spawner.calls) == 1
    spawner.stop_all()
    # The read runs once more on a new server.
    assert tools.kb_search("connect").hits and len(spawner.calls) == 2
    spawner.stop_all()
    with pytest.raises(kbc.KBRequestError, match="stopped during POST /api/batch, so the change can be applied or not"):
        tools.kb_write([{"op": "add_node", "label": "Maybe"}])
    assert len(spawner.calls) == 2
    assert tools.kb_write([{"op": "add_node", "label": "Surely"}]).results and len(spawner.calls) == 3


def test_the_real_graphify_serves_the_store_and_stops_after_a_restart(project):
    tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester")
    started = tools.kb_serve(timeout_s=60)
    try:
        assert started.started and started.nodes == 6 and tools.kb_search("connect").hits
        tools.kb_write([{"op": "add_node", "label": "Billing", "class": "concept"}])
    finally:
        # A new instance, as after a gateway restart, stops the server through kb-serve.json.
        begun = time.monotonic()
        stopped = GraphifyTools(workspace_root=lambda: str(project)).kb_stop()
        (process,) = tools._kb_procs.values()
        process.wait(timeout=5)
    # The stop reaps its exited child at once instead of waiting for a zombie.
    assert stopped.stopped and stopped.pid == started.pid and time.monotonic() - begun < 10
    assert not tools_module._kb_alive(started.pid)
    # The server saved its change before it stopped.
    saved = "".join(path.read_text(errors="replace") for path in (project / "graphify-out").rglob("*.json"))
    assert "Billing" in saved and not (project / "graphify-out" / "kb-serve.json").exists()


# -- sessions -----------------------------------------------------------------------


def test_each_session_keeps_its_source_its_token_and_its_changes(hosted, project, spawner):
    session = {"id": "a"}
    tools = GraphifyTools(
        workspace_root=lambda: str(project), spawn=spawner, kb_actor="tester", kb_session=lambda: session["id"]
    )
    assert tools.kb_use(hosted, token=TOKEN).source == hosted
    mine = tools.kb_write([{"op": "add_node", "label": "From A", "class": "concept"}])
    session["id"] = "b"
    # Session b has its own default source and no token for the server of session a.
    assert tools.kb_sources().current == "local"
    with pytest.raises(kbc.KBRequestError) as error:
        tools.kb_search("From A", source=hosted)
    assert error.value.status == 401
    with pytest.raises(ValueError, match=f"This session has no change in {hosted} to revert"):
        tools.kb_undo(source=hosted)
    tools.kb_use(hosted, token=TOKEN)
    theirs = tools.kb_write([{"op": "add_node", "label": "From B", "class": "concept"}])
    session["id"] = "a"
    # kb_undo reverts the change of this session, not the later change of another session.
    undone = tools.kb_undo()
    assert undone.undone == mine.seqs[0] != theirs.seqs[0]
    labels = {hit.label for hit in tools.kb_search("From").hits}
    assert "From B" in labels and "From A" not in labels
    assert TOKEN not in repr(tools._kb_state()) + str(tools.kb_sources()) + repr(tools.kb_sources())


# -- tokens of a server elsewhere ----------------------------------------------------


def test_use_with_a_token_serves_the_later_calls(hosted, project):
    tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester")
    with pytest.raises(kbc.KBRequestError, match="Give the token with graphify.kb_use") as error:
        tools.kb_search("query", source=hosted)
    assert error.value.status == 401
    used = tools.kb_use(hosted + "/", token=TOKEN)
    assert (used.url, used.started, used.source) == (hosted, False, hosted) and used.nodes > 0
    # The later calls need no source and no token.
    assert tools.kb_search("query").hits[0].id == "src_app_db_query"
    tools.kb_write([{"op": "add_node", "label": "Billing", "class": "concept"}])
    assert tools.kb_node("kb:billing").label == "Billing"
    tools.kb_undo()
    with pytest.raises(kbc.KBRequestError, match="404"):
        tools.kb_node("kb:billing")
    shown = str(used) + repr(used) + repr(tools._kb_state()) + str(presentation.kb_server(used))
    assert TOKEN not in shown
    # kb_use() goes back to the default source: the local store.
    tools._spawn = Spawner()
    try:
        assert tools.kb_use().source == "local" and tools.kb_sources().current == "local"
    finally:
        tools._spawn.stop_all()


def test_use_refuses_a_wrong_token_without_showing_it(hosted, project, monkeypatch):
    tools = GraphifyTools(workspace_root=lambda: str(project))
    with pytest.raises(kbc.KBRequestError, match="needs a token. Give token=") as error:
        tools.kb_use(hosted)
    assert error.value.status == 401
    wrong = "a-wrong-token-that-is-long"
    with pytest.raises(kbc.KBRequestError, match="refused the token[.]") as error:
        tools.kb_use(hosted, token=wrong)
    assert error.value.status == 401 and wrong not in str(error.value)
    monkeypatch.setenv("GRAPHIFY_KB_URL", hosted)
    monkeypatch.setenv("GRAPHIFY_KB_TOKEN", wrong)
    with pytest.raises(kbc.KBRequestError, match="refused the token in GRAPHIFY_KB_TOKEN"):
        tools.kb_use(hosted)
    # A correct token passes also during the login backoff.
    monkeypatch.setenv("GRAPHIFY_KB_TOKEN", TOKEN)
    assert tools.kb_use(hosted).url == hosted
    with pytest.raises(ValueError, match="is a store on this machine. It needs no token"):
        tools.kb_use("local", token=TOKEN)


def test_use_asks_in_a_masked_form_that_checks_each_try(hosted, project):
    cancelled = GraphifyTools(workspace_root=lambda: str(project), kb_ask_token=lambda url, check: None)
    with pytest.raises(kbc.KBRequestError, match="needs a token"):
        cancelled.kb_use(hosted)
    asked = []

    def ask(url, check):
        asked.append(url)
        # The form shows the message of a refused token and closes on the right one.
        assert check("a-wrong-token-that-is-long") == "The server refused this token."
        assert check(TOKEN) is None
        return f" {TOKEN} "

    tools = GraphifyTools(workspace_root=lambda: str(project), kb_ask_token=ask)
    assert tools.kb_use(hosted).url == hosted and asked == [hosted]
    assert tools.kb_search("query").hits


def test_use_reads_a_secret_handle_through_the_host(hosted, project):
    vault = {"handle:1": TOKEN}
    tools = GraphifyTools(workspace_root=lambda: str(project), kb_secret=lambda text: vault.get(text, text))
    assert tools.kb_use(hosted, token="handle:1").url == hosted
    assert tools.kb_use(hosted, token=f" {TOKEN} ").url == hosted


def test_environment_token_goes_only_to_the_configured_server(hosted, project, monkeypatch):
    other = _start(project, token=TOKEN)
    try:
        monkeypatch.setenv("GRAPHIFY_KB_URL", hosted + "/")
        monkeypatch.setenv("GRAPHIFY_KB_TOKEN", TOKEN)
        tools = GraphifyTools(workspace_root=lambda: str(project))
        # The configured server and token serve the calls without kb_use, and kb_use() checks them.
        assert tools.kb_search("query").hits
        assert tools.kb_use().url == hosted
        # A source that a call names never gets the token, also when its server would take it.
        with pytest.raises(kbc.KBRequestError, match="kb_use") as error:
            tools.kb_search("query", source=other.url)
        assert error.value.status == 401
        with pytest.raises(
            kbc.KBRequestError, match=f"The token in GRAPHIFY_KB_TOKEN goes only to {hosted}[.] Give token="
        ):
            tools.kb_use(other.url)
        # An invalid configured server stops only the calls that use it.
        monkeypatch.setenv("GRAPHIFY_KB_URL", "ftp://kb.example.com")
        fresh = GraphifyTools(workspace_root=lambda: str(project))
        with pytest.raises(ValueError, match="GRAPHIFY_KB_URL: ftp://kb.example.com is not an http"):
            fresh.kb_search("query")
        assert fresh.kb_use(other.url, token=TOKEN).url == other.url and fresh.kb_search("query").hits
    finally:
        _stop(other)


def test_settings_pair_their_server_and_token(hosted, project, monkeypatch):
    other = _start(project, token=TOKEN)
    saved = {"url": hosted + "/", "token": f" {TOKEN} "}
    try:
        # A project file can change the environment. Settings win, and their token never goes to another server.
        monkeypatch.setenv("GRAPHIFY_KB_URL", other.url)
        tools = GraphifyTools(workspace_root=lambda: str(project), kb_settings=lambda: (saved["url"], saved["token"]))
        assert tools.kb_search("query").hits
        assert tools.kb_use().url == hosted
        with pytest.raises(kbc.KBRequestError, match=f"The token in Settings goes only to {hosted}[.] Give token="):
            tools.kb_use(other.url)
        # Without a server in Settings, their token stays unused, also for the server in the environment.
        saved["url"] = ""
        fresh = GraphifyTools(workspace_root=lambda: str(project), kb_settings=lambda: (saved["url"], saved["token"]))
        with pytest.raises(kbc.KBRequestError, match="needs a token. Give token=") as error:
            fresh.kb_use()
        assert error.value.status == 401 and TOKEN not in str(error.value)
        saved["url"] = "ftp://kb.example.com"
        with pytest.raises(ValueError, match="graphify_kb_url: ftp://kb.example.com is not an http"):
            fresh.kb_search("query")
        assert fresh.kb_use(hosted, token=TOKEN).url == hosted
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


def test_use_follows_a_redirect_only_on_the_same_host(hosted, project):
    port = hosted.rsplit(":", 1)[1]
    same, same_url = _redirect_server(hosted)
    other, other_url = _redirect_server(f"http://localhost:{port}")
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project))
        assert tools.kb_use(same_url, token=TOKEN).url == hosted and tools.kb_sources().current == hosted
        with pytest.raises(kbc.KBRequestError, match=f"redirects to http://localhost:{port}/api/health") as error:
            tools.kb_use(other_url, token=TOKEN)
        assert error.value.status == 308
        # A write never follows a redirect: urllib would send the token on and turn the POST into a GET.
        with pytest.raises(kbc.KBRequestError, match="redirects to"):
            tools.kb_write([{"op": "add_node", "label": "X"}], source=same_url)
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
    assert "kb_serve" not in str(error.value)


# -- snapshots and pages ---------------------------------------------------------------


def test_snapshot_copies_a_server_into_a_read_only_local_source(project, tmp_path_factory, spawner):
    remote = _start(_other_project(project, tmp_path_factory), token=TOKEN)
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawner, kb_actor="tester")
        tools.kb_use(remote.url, token=TOKEN)
        tools.kb_write(
            [
                {"op": "add_node", "label": "Billing", "class": "concept"},
                {"op": "add_document", "title": "Billing notes", "body": "[[Billing]] uses query()."},
            ]
        )
        snap = tools.kb_snapshot()
        name = f"graphify-out/snapshots/127.0.0.1-{remote.port}"
        # The copy has the 6 code nodes, the concept and the node of the document.
        assert (snap.source, snap.name, snap.nodes, snap.documents, snap.assets) == (remote.url, name, 8, 1, 0)
        assert snap.version > 0 and Path(snap.path) == (project / name).resolve()
        assert str(snap).endswith(f"Read it with source={name!r}. The copy is read-only.")
        assert presentation.kb_snapshot(snap).summary.startswith(f"{remote.url} to {name}: 8 nodes,")
        meta = json.loads((project / name / "snapshot.json").read_text())
        assert (meta["source"], meta["version"], meta["assets"]) == (remote.url, snap.version, False)
        assert not any(TOKEN in f.read_text(errors="replace") for f in (project / name).rglob("*") if f.is_file())
        # The copy answers without the remote server, through a read-only server of this machine.
        assert "kb:billing" in {hit.id for hit in tools.kb_search("Billing", source=name).hits}
        assert spawner.calls[-1][0][-1] == "--read-only" and tools.kb_node("kb:billing", source=name).kb
        with pytest.raises(ValueError, match="is a read-only snapshot. Write to the source that it copies"):
            tools.kb_write([{"op": "add_node", "label": "X"}], source=name)
        with pytest.raises(ValueError, match="is a snapshot. Copy the source that it copies"):
            tools.kb_snapshot(name)
        # A new snapshot replaces the copy, and the running server of the copy reads the new files.
        tools.kb_write([{"op": "add_node", "label": "Invoices", "class": "concept"}])
        again = tools.kb_snapshot()
        assert again.name == name and again.version > snap.version and again.nodes == 9
        assert tools.kb_search("Invoices", source=name).hits and len(spawner.calls) == 1
        assert not [p for p in (project / "graphify-out" / "snapshots").iterdir() if p.name.startswith(".")]
        sources = tools.kb_sources()
        rows = {row.source: row for row in sources.sources}
        assert sources.current == remote.url and rows[remote.url].current
        assert (rows["local"].kind, rows[name].kind) == ("local", "snapshot")
        assert rows[name].note.startswith(f"a copy of {remote.url} from ") and rows[name].url.startswith("http://")
        assert presentation.kb_sources(sources).summary == f"{len(rows)} sources, current {remote.url}"
        # kb_use makes the copy the source of the session; the remote server can go away.
        tools.kb_use(name)
        _stop(remote)
        assert tools.kb_sources().current == name and tools.kb_search("Invoices").source == name
        assert tools.kb_stop(name).stopped
    finally:
        try:
            _stop(remote)
        except OSError:
            pass


def test_snapshot_export_refuses_unsafe_files(tmp_path):
    from vis_graphify.tools import _kb_write_export

    def export(*files):
        return {"format": "graphify-kb-export", "files": list(files)}

    graph = {"path": "graph.json", "json": {"nodes": [{"id": "a"}], "links": []}}
    with pytest.raises(kbc.KBRequestError, match="sent no store export"):
        _kb_write_export(tmp_path / "a", {"files": []})
    for bad in ("../escape.json", "/etc/x", "kb\\x", "c:x", ""):
        with pytest.raises(kbc.KBRequestError, match="unsafe path"):
            _kb_write_export(tmp_path / "b", export(graph, {"path": bad, "text": "x"}))
    with pytest.raises(kbc.KBRequestError, match="has no graph.json"):
        _kb_write_export(tmp_path / "c", export({"path": "kb/overlay.json", "json": {}}))
    meta = {"path": "kb/documents/d1/meta.json", "json": {"title": "D"}}
    asset = {"path": "kb/documents/d1/a.png", "base64": "aGk="}
    counts = _kb_write_export(tmp_path / "d", export(graph, meta, asset))
    assert counts == {"nodes": 1, "edges": 0, "documents": 1, "assets": 1}
    assert (tmp_path / "d" / "kb" / "documents" / "d1" / "a.png").read_bytes() == b"hi"


def test_publish_writes_the_page_of_the_local_store_with_its_unsaved_changes(project):
    # The server keeps changes in memory for an hour, so only the flush of kb_publish saves them.
    spawn = Spawner(flush_interval=3600)
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawn)
        tools.kb_write([{"op": "add_node", "label": "Unsaved idea", "class": "concept"}])
        published = tools.kb_publish()
        assert Path(published.path) == (project / "graphify-out" / "kb.html").resolve() and published.bytes > 1000
        assert published.source == "local" and "Unsaved idea" in Path(published.path).read_text()
        with pytest.raises(ValueError, match="path must be an .html file name without folders"):
            tools.kb_publish(path="../escape.html")
    finally:
        spawn.stop_all()


def test_publish_copies_a_server_first(served, project):
    tools, url = served
    tools.kb_write([{"op": "add_node", "label": "Remote idea", "class": "concept"}], source=url)
    published = tools.kb_publish(source=url)
    page = project / "graphify-out" / f"kb-127.0.0.1-{url.rsplit(':', 1)[1]}.html"
    assert Path(published.path) == page.resolve() and published.source == url and published.bytes > 1000
    assert "Remote idea" in page.read_text()


# -- moved code nodes ----------------------------------------------------------------------


def test_moved_nodes_reach_the_server_of_the_session(project, tmp_path_factory):
    server = _start(_other_project(project, tmp_path_factory))
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester")
        tools.kb_use(server.url)
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


def test_moved_nodes_reach_the_project_store_and_another_store_of_the_session(project, tmp_path_factory, spawner):
    _with_records(project)
    other = _other_project(project, tmp_path_factory)
    _with_records(other)
    runs = []

    def run(argv, **kwargs):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, 0, '[{"status": "moved"}]', "")

    tools = GraphifyTools(workspace_root=lambda: str(project), runner=run, spawn=spawner)
    moved = {"src_app_db_query": "src_app_db_fetch"}
    assert tools._kb_moves(project.resolve(), moved) == (
        "graphify-out/kb: 1 moved, 0 pending, 0 with no records at the old id",
    )
    tools.kb_use(str(other))
    lines = tools._kb_moves(project.resolve(), moved)
    assert len(lines) == 2 and lines[1].endswith(": 1 moved, 0 pending, 0 with no records at the old id")
    assert [argv[argv.index("--graph") + 1] for argv in runs[1:]] == [
        str((project / "graphify-out" / "graph.json").resolve()),
        str((other / "graphify-out" / "graph.json").resolve()),
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


def test_moved_nodes_report_a_source_that_fails_and_skip_a_snapshot(project, monkeypatch, tmp_path_factory):
    tools = GraphifyTools(workspace_root=lambda: str(project))
    moved = {"src_app_db_query": "src_app_db_fetch"}
    dead = f"http://127.0.0.1:{_free_port()}"
    monkeypatch.setenv("GRAPHIFY_KB_URL", dead)
    (line,) = tools._kb_moves(project.resolve(), moved)
    assert line.startswith(f"{dead}: not updated: No knowledge base server answers at {dead}")
    monkeypatch.setenv("GRAPHIFY_KB_URL", "ftp://kb.example.com")
    assert tools._kb_moves(project.resolve(), moved) == (
        "knowledge source: not updated: GRAPHIFY_KB_URL: ftp://kb.example.com is not an http or https URL.",
    )
    copy = _other_project(project, tmp_path_factory)
    (copy / "graphify-out" / "snapshot.json").write_text("{}")
    tools._kb_state().source = str((copy / "graphify-out" / "graph.json").resolve())
    assert tools._kb_moves(project.resolve(), moved) == ()
