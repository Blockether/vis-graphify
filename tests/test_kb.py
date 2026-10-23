import threading
from pathlib import Path

import pytest
from graphify.kb import KnowledgeBase
from graphify.kb_server import KBServer

from vis_graphify import kb as kbc
from vis_graphify import presentation
from vis_graphify.tools import GraphifyTools


def _start(project, **options):
    knowledge = KnowledgeBase(project / "graphify-out" / "graph.json", autoflush=False)
    server = KBServer(knowledge, "127.0.0.1", options.pop("port", 0), flush_interval=0.05, **options)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


@pytest.fixture
def served(project):
    server = _start(project)
    tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester")
    yield tools, server.url
    server.shutdown()
    server.server_close()


def test_search_and_node_read_the_server(served):
    tools, url = served
    found = tools.kb_search("query", url=url)
    assert found.hits and found.hits[0].id == "src_app_db_query"
    node = tools.kb_node("src_app_db_query", url=url)
    # Since graphify v0.9.93-blockether.12, an extracted function has the class code.function.
    assert (node.label, node.cls, node.kb) == ("query()", "code.function", False)
    assert {(e.direction, e.relation, e.other) for e in node.edges} >= {
        ("out", "calls", "src_app_db_connect"),
        ("in", "calls", "src_app_core_run"),
    }
    assert tools.kb_node("connect()", url=url).id == "src_app_db_connect"
    assert presentation.kb_node(node).summary.startswith("query() (code.function)")
    # A class filter also finds the subclasses: code finds the code.function node.
    assert tools.kb_search("query", url=url, cls="code").hits[0].id == "src_app_db_query"


def test_search_without_matches_is_empty(served):
    tools, url = served
    found = tools.kb_search("zzz-nothing", url=url)
    assert found.hits == () and "Nothing" in str(found)
    assert presentation.kb_search(found).summary == "no matches"


def test_write_is_one_undoable_change(served):
    tools, url = served
    change = tools.kb_write(
        [
            {"op": "add_node", "label": "Billing", "class": "concept"},
            {"op": "add_edge", "source": "kb:billing", "target": "src_app_db_query", "relation": "uses"},
            {"op": "add_document", "title": "Billing notes", "body": "[[Billing]] uses query()."},
        ],
        url=url,
    )
    assert change.summary == "Applied 1 add_node, 1 add_edge, 1 add_document"
    assert change.results[0]["id"] == "kb:billing" and change.version > 0
    node = tools.kb_node("kb:billing", url=url)
    assert node.kb and node.cls == "concept"
    assert "Billing notes" in {hit.label for hit in tools.kb_search("Billing", url=url).hits}
    undone = tools.kb_undo(url=url)
    assert undone.undone is not None and "Batch of 3" in undone.summary
    assert str(undone).startswith(f"Undid change {undone.undone}: Batch of 3")
    with pytest.raises(kbc.KBRequestError) as error:
        tools.kb_node("kb:billing", url=url)
    assert error.value.status == 404


def test_write_failure_applies_nothing(served):
    tools, url = served
    with pytest.raises(kbc.KBRequestError, match="unknown op 'nope'") as error:
        tools.kb_write([{"op": "add_node", "label": "Kept out"}, {"op": "nope"}], url=url)
    assert error.value.status == 400
    assert tools.kb_search("Kept out", url=url).hits == ()
    with pytest.raises(ValueError):
        tools.kb_write([], url=url)


def test_ingest_text_file_and_inline_text(served, project):
    tools, url = served
    (project / "notes.md").write_text("# Database\nquery() calls connect() on start.\n")
    result = tools.kb_ingest("notes.md", url=url, tags=["db"])
    assert result.document_id and result.mode == "text"
    assert "query()" in result.linked or "connect()" in result.linked
    inline = tools.kb_ingest("inline.md", url=url, text="# Inline\nNo link here.", link=False)
    assert inline.title == "Inline" and inline.linked == ()
    assert presentation.kb_ingest(result).headline == "Ingest document"
    with pytest.raises(FileNotFoundError):
        tools.kb_ingest("missing.md", url=url)
    with pytest.raises(ValueError):
        tools.kb_ingest("notes.md", url=url, mode="magic")


def test_validate_and_publish(served, project):
    tools, url = served
    # graphify reports an orphan only for a node that the knowledge base added.
    tools.kb_write([{"op": "add_node", "label": "Loose idea", "class": "concept"}], url=url)
    report = tools.kb_validate(url=url, info=True)
    assert report.notes >= 1 and report.by_code.get("orphan_node", 0) >= 1
    assert presentation.kb_validate(report).content[0].columns[0] == "Severity"
    published = tools.kb_publish(url=url)
    assert Path(published.path) == (project / "graphify-out" / "kb.html").resolve() and published.bytes > 1000
    with pytest.raises(kbc.KBRequestError, match="inside the graphify output folder"):
        tools.kb_publish(url=url, path="../escape.html")


def test_read_only_server_refuses_writes(project):
    server = _start(project, read_only=True)
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project))
        with pytest.raises(kbc.KBRequestError, match="read-only") as error:
            tools.kb_write([{"op": "add_node", "label": "X"}], url=server.url)
        assert error.value.status == 403
    finally:
        server.shutdown()
        server.server_close()


def test_missing_server_names_kb_serve(project):
    tools = GraphifyTools(workspace_root=lambda: str(project))
    with pytest.raises(kbc.KBRequestError, match="graphify.kb_serve"):
        tools.kb_search("x", url="http://127.0.0.1:9")


class FakeProcess:
    def __init__(self, server=None, code=None):
        self.server, self.code, self.pid, self.terminated = server, code, 4242, False

    def poll(self):
        return self.code

    @property
    def returncode(self):
        return self.code

    def terminate(self):
        self.terminated = True
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def wait(self, timeout=None):
        return 0


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_starts_graphify_then_reuses_and_stops(project):
    port = _free_port()
    calls = []

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess(_start(project, port=port))

    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawn)
    started = tools.kb_serve(port=port)
    argv, kwargs = calls[0]
    assert (
        argv[-6:]
        == [
            "serve",
            str(project.resolve() / "graphify-out" / "graph.json"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ][-6:]
    )
    assert kwargs["start_new_session"] and kwargs["cwd"] == str(project.resolve())
    assert started.started and started.nodes == 6 and started.pid == 4242
    assert started.log_path.endswith("kb-serve.log") and "admin UI" in str(started)
    assert tools.kb_search("connect").hits
    again = tools.kb_serve(port=port)
    assert not again.started and len(calls) == 1
    assert presentation.kb_server(again).headline == "Use knowledge base"
    stopped = tools.kb_stop()
    assert stopped.url.endswith(str(port))
    # E2E report bug 22: the stop result said "Using knowledge base ... Open the URL".
    assert stopped.stopped and str(stopped).startswith(f"Stopped knowledge base {stopped.url}.")
    assert "Open the URL" not in str(stopped)
    with pytest.raises(RuntimeError, match="did not start"):
        tools.kb_stop(url=stopped.url)


def test_serve_reports_a_crashed_server(project):
    def spawn(argv, stdout=None, **kwargs):
        stdout.write(b"error: address in use\n")
        return FakeProcess(code=1)

    tools = GraphifyTools(workspace_root=lambda: str(project), spawn=spawn)
    with pytest.raises(RuntimeError, match="status 1") as error:
        tools.kb_serve(port=_free_port())
    assert "address in use" in str(error.value)


def test_serve_refuses_a_port_with_another_graph(project, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    (other / "graphify-out").mkdir()
    (other / "graphify-out" / "graph.json").write_text((project / "graphify-out" / "graph.json").read_text())
    server = _start(other)
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project), spawn=lambda *a, **k: pytest.fail("no spawn"))
        with pytest.raises(RuntimeError, match="already serves"):
            tools.kb_serve(port=server.port)
    finally:
        server.shutdown()
        server.server_close()
