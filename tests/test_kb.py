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


TOKEN = "kb-token-for-the-tests-0123"


@pytest.fixture
def hosted(project, monkeypatch):
    """A server that needs a token, as on a host. Each test gets a new server and a new login backoff."""
    monkeypatch.delenv("GRAPHIFY_KB_TOKEN", raising=False)
    monkeypatch.delenv("GRAPHIFY_KB_URL", raising=False)
    server = _start(project, token=TOKEN)
    yield server.url
    server.shutdown()
    server.server_close()


def test_connect_with_a_token_serves_the_later_calls(hosted, project):
    tools = GraphifyTools(workspace_root=lambda: str(project), kb_actor="tester")
    with pytest.raises(kbc.KBRequestError, match="kb_connect") as error:
        tools.kb_search("query", url=hosted)
    assert error.value.status == 401
    connected = tools.kb_connect(hosted + "/", token=TOKEN)
    assert (connected.url, connected.started) == (hosted, False) and connected.nodes > 0
    # The later calls need no url and no token.
    assert tools.kb_search("query").hits[0].id == "src_app_db_query"
    tools.kb_write([{"op": "add_node", "label": "Billing", "class": "concept"}])
    assert tools.kb_node("kb:billing").label == "Billing"
    tools.kb_undo()
    with pytest.raises(kbc.KBRequestError, match="404"):
        tools.kb_node("kb:billing")
    shown = str(connected) + repr(connected) + repr(tools._kb(None)) + str(presentation.kb_server(connected))
    assert TOKEN not in shown


def test_connect_refuses_a_wrong_token_without_showing_it(hosted, project, monkeypatch):
    tools = GraphifyTools(workspace_root=lambda: str(project))
    with pytest.raises(kbc.KBRequestError, match="needs a token. Give token=") as error:
        tools.kb_connect(hosted)
    assert error.value.status == 401
    wrong = "a-wrong-token-that-is-long"
    with pytest.raises(kbc.KBRequestError, match="refused the token[.]") as error:
        tools.kb_connect(hosted, token=wrong)
    assert error.value.status == 401 and wrong not in str(error.value)
    monkeypatch.setenv("GRAPHIFY_KB_TOKEN", wrong)
    with pytest.raises(kbc.KBRequestError, match="refused the token in GRAPHIFY_KB_TOKEN"):
        tools.kb_connect(hosted)
    # A correct token passes also during the login backoff.
    monkeypatch.setenv("GRAPHIFY_KB_TOKEN", TOKEN)
    assert tools.kb_connect(hosted).url == hosted


def test_connect_asks_in_a_masked_form_that_checks_each_try(hosted, project):
    cancelled = GraphifyTools(workspace_root=lambda: str(project), kb_ask_token=lambda url, check: None)
    with pytest.raises(kbc.KBRequestError, match="needs a token"):
        cancelled.kb_connect(hosted)
    asked = []

    def ask(url, check):
        asked.append(url)
        # The form shows the message of a refused token and closes on the right one.
        assert check("a-wrong-token-that-is-long") == "The server refused this token."
        assert check(TOKEN) is None
        return f" {TOKEN} "

    tools = GraphifyTools(workspace_root=lambda: str(project), kb_ask_token=ask)
    assert tools.kb_connect(hosted).url == hosted and asked == [hosted]
    assert tools.kb_search("query").hits


def test_connect_reads_a_secret_handle_through_the_host(hosted, project):
    vault = {"handle:1": TOKEN}
    tools = GraphifyTools(workspace_root=lambda: str(project), kb_secret=lambda text: vault.get(text, text))
    assert tools.kb_connect(hosted, token="handle:1").url == hosted
    assert tools.kb_connect(hosted, token=f" {TOKEN} ").url == hosted


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


def test_connect_follows_a_redirect_only_on_the_same_host(hosted, project):
    port = hosted.rsplit(":", 1)[1]
    same, same_url = _redirect_server(hosted)
    other, other_url = _redirect_server(f"http://localhost:{port}")
    try:
        tools = GraphifyTools(workspace_root=lambda: str(project))
        assert tools.kb_connect(same_url, token=TOKEN).url == hosted
        with pytest.raises(kbc.KBRequestError, match=f"redirects to http://localhost:{port}/api/health") as error:
            tools.kb_connect(other_url, token=TOKEN)
        assert error.value.status == 308
        # A write never follows a redirect: urllib would send the token on and turn the POST into a GET.
        with pytest.raises(kbc.KBRequestError, match="redirects to"):
            tools.kb_write([{"op": "add_node", "label": "X"}], url=same_url)
    finally:
        for server in (same, other):
            server.shutdown()
            server.server_close()


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


def test_client_keeps_the_base_path_and_explains_hosted_errors():
    import io
    import urllib.error

    sent = []

    def answer(request, timeout):
        sent.append((request.full_url, request.get_header("Authorization")))
        return io.BytesIO(b'{"ok": true}')

    client = kbc.KBClient(url="https://kb.example.com/team", token=TOKEN, _opener=answer)
    assert client.request("GET", "/api/stats") == {"ok": True}
    assert sent == [("https://kb.example.com/team/api/stats", f"Bearer {TOKEN}")]
    assert TOKEN not in repr(client)

    def refuse(code, body, headers=None):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "refused", headers or {}, io.BytesIO(body))

        return kbc.KBClient(url="https://kb.example.com", _opener=opener)

    with pytest.raises(kbc.KBRequestError, match="--allow-host kb.example.com"):
        refuse(403, b'{"error": "unknown Host header"}').request("GET", "/api/stats")
    with pytest.raises(kbc.KBRequestError, match="wait and try again [(]after 3 s[)]"):
        refuse(429, b'{"error": "too many failed logins; wait and try again"}', {"Retry-After": "3"}).request(
            "GET", "/api/stats"
        )

    def down(request, timeout):
        raise urllib.error.URLError("no such host")

    with pytest.raises(kbc.KBRequestError, match="Check the URL") as error:
        kbc.KBClient(url="https://kb.example.com", _opener=down).request("GET", "/api/health")
    assert "kb_serve" not in str(error.value)


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
