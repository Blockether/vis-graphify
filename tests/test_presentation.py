import dataclasses
import importlib.util
from pathlib import Path

import blockether.vis.extension as vis
import pytest

from vis_graphify import presentation
from vis_graphify.tools import (
    BuildResult,
    CallEdit,
    DomainFinding,
    DomainReport,
    Edge,
    Explanation,
    GraphifyTools,
    HubsResult,
    ImpactResult,
    MoveResult,
    Node,
    PathResult,
    Reference,
    ReferencesResult,
    RenameEdit,
    RenameResult,
    SearchResult,
    SignatureResult,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tools(project):
    return GraphifyTools(workspace_root=lambda: str(project))


def summary(presented):
    assert isinstance(presented, vis.ActivityPresentation)
    return presented.summary


def test_success_states(tools, project):
    assert summary(presentation.status(tools.status())) == "6 nodes, 4 edges, 4 files"
    assert summary(presentation.search(tools.search("db", limit=1))) == "1 of 2 matches for 'db'"
    assert summary(presentation.explain(tools.explain("app.core/run"))) == "run(): 1 outgoing, 2 incoming"
    assert summary(presentation.shortest_path(tools.shortest_path("app.web/run", "connect"))) == (
        "3 hops: run() → connect()"
    )
    assert summary(presentation.hubs(tools.hubs(limit=2))) == "top 2, led by run()"
    impact = presentation.affected(tools.affected("connect", depth=3))
    assert summary(impact) == "connect(): 3 functions in 3 files"
    assert impact.content[0].text == "Functions: query, run"
    assert impact.content[1].columns == ("Node", "Location", "Steps", "Through")
    assert impact.content[1].rows[-1][-1] == "run() → query()"
    build = BuildResult(str(project), "g.json", 6, 4, 1.5)
    assert summary(presentation.build(build)) == "6 nodes, 4 edges in 1.5 s"


def test_empty_states(tools, tmp_path_factory):
    empty = GraphifyTools(workspace_root=lambda: str(tmp_path_factory.mktemp("empty")))
    assert summary(presentation.status(empty.status())) == "no graph yet"
    assert summary(presentation.search(SearchResult("zzz", 0, ()))) == "no matches"
    assert summary(presentation.hubs(HubsResult(()))) == "no edges"
    lonely = tools.explain("lonely")
    assert summary(presentation.explain(Explanation(lonely.node, (), (), False))) == "lonely(): 0 outgoing, 0 incoming"
    assert summary(presentation.shortest_path(PathResult(lonely.node, lonely.node, False, ()))) == "not connected"
    assert summary(presentation.affected(ImpactResult(lonely.node, 2, (), (), False))) == "no dependents"


def test_renderer_failure_and_start_show_arguments():
    render = presentation.renderer("Explain node", presentation.explain, ("node", "root"))
    failed = render(phase="failure", args=(object(), "run"), kwargs={"root": "."}, error=ValueError("ambiguous"))
    assert failed.summary == "failed"
    texts = [getattr(block, "text", "") for block in failed.content]
    assert "node='run', root='.'" in texts and "ambiguous" in texts
    started = render(phase="start", args=("run",), kwargs={})
    assert started.summary == "running"
    assert render(phase="start", args=(), kwargs={}) is None
    assert render(phase="success", result=None) is None


def test_knowledge_states():
    from vis_graphify.kb import KBHit, KBServer, KBSnapshot

    place = "graphify-out/snapshots/kb.example.com"
    snap = KBSnapshot("https://kb.example.com", place, f"/p/{place}", 7, 9, 3, 1, 2, "2025-01-01T00:00:00Z")
    assert summary(presentation.snapshot(snap)) == (
        f"https://kb.example.com to {place}: 9 nodes, 3 edges, 1 document, 2 document files"
    )
    render = presentation.renderer("Snapshot knowledge base", presentation.snapshot, ("source", "name", "assets"))
    failed = render(phase="failure", args=(object(), place), kwargs={}, error=ValueError("is a snapshot"))
    texts = [getattr(block, "text", "") for block in failed.content]
    assert failed.summary == "failed" and f"source={place!r}" in texts and "is a snapshot" in texts
    # search: code and knowledge, knowledge alone, and the note of a source that did not answer.
    node = Node("a", "run()", "src/a.py", 3, "function", None)
    hit = KBHit("document", "d1", "Billing notes", "", "graphify-out/kb/documents/d1/body.md", 2.5)
    both = presentation.search(SearchResult("bill", 1, (node,), knowledge=(hit,), source="local"))
    assert summary(both) == "1 code and 1 knowledge matches for 'bill'"
    assert [block.text for block in both.content[::2]] == ["Code", "Knowledge in local"]
    assert both.content[3].rows == (("Billing notes", "document", "", "graphify-out/kb/documents/d1/body.md"),)
    assert summary(presentation.search(SearchResult("bill", 0, (), knowledge=(hit,), source="local"))) == (
        "1 knowledge match for 'bill'"
    )
    note = "No knowledge from https://kb.example.com: no answer"
    missing = presentation.search(SearchResult("zzz", 0, (), source="https://kb.example.com", note=note))
    assert summary(missing) == "no matches" and missing.content[-1].text == note
    # explain: a document shows its attributes and text. A knowledge neighbour has no file.
    other = Node("kb:x", "X", "", None, "knowledge", None)
    document = Explanation(
        Node("d1", "Billing notes", "", None, "document", None),
        (Edge("links", node, "EXTRACTED", "", knowledge=True), Edge("links", other, "EXTRACTED", "", knowledge=True)),
        (),
        False,
        attributes={"title": "Billing notes", "body": "Text", "tags": ["a"]},
        source="local",
    )
    shown = presentation.explain(document)
    assert summary(shown) == "Billing notes: 2 outgoing, 0 incoming"
    assert shown.content[0].text == "Billing notes (document) [d1] in local"
    assert shown.content[1].rows == (("title", "Billing notes"), ("tags", "['a']"))
    assert [block.text for block in shown.content[2:4]] == ["Text", "Text"]
    assert shown.content[-1].rows == (("links", "run()", "src/a.py:3"), ("links", "X", "knowledge"))
    # serve: a started server, a running one and a stopped one.
    server = KBServer("http://127.0.0.1:5000", "/p/graph.json", 7, 3, 1, 4, False, True, source="local")
    assert (presentation.serve(server).headline, summary(presentation.serve(server))) == (
        "Start knowledge base",
        "local at http://127.0.0.1:5000: 7 nodes, 3 edges, 1 document",
    )
    used = presentation.serve(dataclasses.replace(server, started=False, read_only=True))
    assert used.headline == "Use knowledge base" and used.summary.startswith(
        "local at http://127.0.0.1:5000, read-only:"
    )
    stopped = presentation.serve(dataclasses.replace(server, stopped=True))
    assert (stopped.headline, stopped.summary) == ("Stop knowledge base", "stopped local at http://127.0.0.1:5000")


def _entry(monkeypatch):
    """Load extension.py with a stub registration. The test end restores the unwrapped GraphifyTools methods."""
    for name, value in list(vars(GraphifyTools).items()):
        if not name.startswith("_"):
            monkeypatch.setattr(GraphifyTools, name, value)
    registered = []
    monkeypatch.setattr(vis, "register_extension", registered.append)
    spec = importlib.util.spec_from_file_location("vis_graphify_entry", ROOT / "extension.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, registered


def test_prompt_names_the_configured_knowledge_base_server(monkeypatch):
    module, registered = _entry(monkeypatch)
    (extension,) = registered
    monkeypatch.delenv("GRAPHIFY_KB_URL", raising=False)
    assert extension.prompt({}) == module.PROMPT
    monkeypatch.setenv("GRAPHIFY_KB_URL", "KB.example.com/")
    assert extension.prompt({}).endswith(
        "\nKnowledge comes from the configured server https://kb.example.com; the code stays local."
    )
    assert "kb_" not in module.PROMPT
    # An invalid address adds nothing: the knowledge calls report it.
    monkeypatch.setenv("GRAPHIFY_KB_URL", "ftp://kb.example.com")
    assert extension.prompt({}) == module.PROMPT


def test_settings_name_the_server_and_keep_the_token_secret(monkeypatch):
    saved = {}

    class Setting:
        def __init__(self, **spec):
            self.spec = spec

        def value(self):
            return saved.get(self.spec["id"], self.spec["default"])

    monkeypatch.setattr(vis, "Setting", Setting)
    module, registered = _entry(monkeypatch)
    (extension,) = registered
    url, token, store = module.SETTINGS
    # The settings keep the default scope, global only: a project file cannot change them.
    assert (url.spec["id"], url.spec["type"], "scopes" in url.spec) == ("graphify_kb_url", "string", False)
    assert (token.spec["id"], token.spec["default"], token.spec["is_secret"]) == ("graphify_kb_token", "", True)
    assert (store.spec["id"], store.spec["default"], "scopes" in store.spec) == ("graphify_kb_store", "", False)
    assert list(extension.settings) == module.SETTINGS
    # The server in Settings wins over the environment.
    monkeypatch.setenv("GRAPHIFY_KB_URL", "other.example.com")
    saved.update(graphify_kb_url=" KB.example.com/ ", graphify_kb_token="t" * 20)
    assert "the configured server https://kb.example.com;" in extension.prompt({})
    assert module._kb_settings() == (" KB.example.com/ ", "t" * 20)
    assert module._kb_store() == ""
    saved.update(graphify_kb_store=" memory ")
    assert module._kb_store() == " memory "


def test_hosts_without_string_settings_keep_the_environment(monkeypatch):
    class Setting:
        """A Setting of a Vis release before text settings: it has no `is_secret`."""

        def __init__(self, *, id, label, default, type="boolean", description=None):
            raise AssertionError("a host without text settings gets no declaration")

    monkeypatch.setattr(vis, "Setting", Setting)
    module, registered = _entry(monkeypatch)
    (extension,) = registered
    assert module.SETTINGS == [] and module._kb_settings() == ("", "") and module._kb_store() == ""
    monkeypatch.setenv("GRAPHIFY_KB_URL", "kb.example.com")
    assert "the configured server https://kb.example.com;" in extension.prompt({})


def test_a_declaration_error_on_a_host_with_text_settings_fails_the_load(monkeypatch):
    def refuse(**spec):
        raise ValueError("A text setting requires a string default")

    monkeypatch.setattr(vis, "Setting", refuse)
    with pytest.raises(ValueError, match="string default"):
        _entry(monkeypatch)


def test_extension_registers_every_tool_with_an_activity(monkeypatch):
    module, registered = _entry(monkeypatch)
    (extension,) = registered
    assert extension.name == "vis-graphify" and extension.alias == "graphify"
    assert extension.version == module.VERSION
    public = [name for name in vars(GraphifyTools) if not name.startswith("_")]
    assert sorted(public) == sorted(
        [
            "build",
            "status",
            "search",
            "explain",
            "shortest_path",
            "hubs",
            "affected",
            "references",
            "rename",
            "add_parameter",
            "move",
            "move_file",
            "domain",
            "serve",
            "snapshot",
            "write",
            "ingest",
            "validate",
            "undo",
            "publish",
        ]
    )
    for name in public:
        method = getattr(GraphifyTools, name)
        assert method.__doc__, name


def test_extension_reads_the_kb_token_in_a_masked_form(monkeypatch):
    module, _ = _entry(monkeypatch)
    vault, forgotten, asked = {"vis-secret:1": "kb-token-0123456789"}, [], []
    monkeypatch.setattr(vis, "forget", forgotten.append)

    class Answer(dict):
        def reveal(self, name):
            return vault.get(self[name])

    def ask(title, fields, **options):
        asked.append(fields)
        return Answer(token="vis-secret:1")

    def check(text):
        return None

    monkeypatch.setattr(vis, "ask", ask)
    assert module._ask_kb_token("https://kb.example.com", check) == "kb-token-0123456789"
    ((field,),) = asked
    assert (field["type"], field["is_required"], field["validate"]) == ("password", True, check)
    # The plaintext leaves the vault as soon as the extension has read it.
    assert forgotten == ["vis-secret:1"]
    # A cancelled, timed-out or undeliverable form answers falsy.
    monkeypatch.setattr(vis, "ask", lambda title, fields, **options: Answer())
    assert module._ask_kb_token("https://kb.example.com", check) is None


def test_versions_agree():
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    text = (ROOT / "extension.py").read_text()
    assert f'VERSION = "{declared}"' in text


def _report(plugins=("edd-core",), findings=(), plugin_errors=()):
    return DomainReport(
        "/p",
        "/p/graphify-out/domain/DOMAIN.md",
        "/p/d.json",
        plugins,
        ("orders-svc",),
        {"command": 2, "event": 1},
        3,
        1,
        1,
        0,
        1,
        findings,
        plugin_errors,
        False,
    )


def test_domain_states():
    missing = DomainFinding("error", "missing-command", "No `release-stok`.", "a.clj", 17, "command:x:y", "edd-core")
    note = DomainFinding("info", "query-unused", "unused", "", None, "", "edd-core")
    presented = presentation.domain(_report(findings=(missing, note)))
    assert summary(presented) == "edd-core: 1 service, 2 commands, 1 event, 1 error, 0 warnings"
    (table,) = [block for block in presented.content if isinstance(block, vis.ActivityTable)]
    assert table.rows == (("error", "missing-command", "No `release-stok`.", "a.clj:17"),)
    empty = presentation.domain(_report(plugins=(), plugin_errors=("bad.py: boom",)))
    assert summary(empty) == "no plugin applies"
    assert "bad.py: boom" in [getattr(block, "text", "") for block in empty.content]
    clean = presentation.domain(_report())
    assert not [block for block in clean.content if isinstance(block, vis.ActivityTable)]


def test_reference_and_rename_states(tools):
    node = tools.explain("connect").node
    ref = Reference("src/app/db.clj", 9, 7, "definition", "connect", False)
    found = ReferencesResult(node, 1, ("src/app/db.clj",), (ref,), False)
    assert summary(presentation.references(found)) == "1 reference in 1 file"
    assert summary(presentation.references(ReferencesResult(node, 0, (), (), False))) == "no references"
    edit = RenameEdit("src/app/db.clj", 9, 7, "connect", "open")
    preview = RenameResult(
        node, "connect", "open", False, ("src/app/db.clj",), (edit,), (), ("src/app/db.clj:2 connect",), 1, "-a\n+b\n"
    )
    assert summary(presentation.rename(preview)) == "connect → open: preview, 1 edit in 1 file"
    done = RenameResult(node, "connect", "open", True, ("src/app/db.clj",), (edit,), (), (), 0, "-a\n+b\n")
    assert summary(presentation.rename(done)) == "connect → open: renamed, 1 edit in 1 file"
    checked = dataclasses.replace(done, check="tsc --noEmit -p .: passed")
    assert summary(presentation.rename(checked)) == "connect → open: renamed, 1 edit in 1 file, tsc passed"
    blocked = RenameResult(node, "connect", "run", False, (), (), ("x already uses `run`.",), (), 0, "")
    assert summary(presentation.rename(blocked)) == "connect → run: not written, 1 conflict"


def test_move_states():
    files = ("app/main.py", "app/text.py", "app/util.py")
    preview = MoveResult(
        "slugify", "app/util.py", "app/text.py", False, files, ("app/text.py",), (), (), (), (), (), 0, "-a\n+b\n"
    )
    assert summary(presentation.move(preview)) == "slugify: app/util.py → app/text.py: preview, 3 files"
    done = dataclasses.replace(preview, applied=True, check="tsc --noEmit -p .: passed")
    assert summary(presentation.move(done)) == "slugify: app/util.py → app/text.py: moved, 3 files, tsc passed"
    renamed = dataclasses.replace(done, subject="app.util", renames=("app.util-test -> app.text-test",), check="")
    moved = presentation.move_file(renamed)
    assert moved.headline == "Move file" and summary(moved) == "app.util: app/util.py → app/text.py: moved, 3 files"
    assert "Renamed: app.util-test -> app.text-test" in [getattr(block, "text", "") for block in moved.content]
    blocked = dataclasses.replace(preview, files=(), conflicts=("app/text.py already defines `slugify`.",), diff="")
    assert summary(presentation.move(blocked)) == "slugify: app/util.py → app/text.py: not written, 1 conflict"
    assert "Blocked move" in str(blocked) and "Nothing was written" in str(blocked)
    assert str(preview).startswith("Preview move of slugify: app/util.py -> app/text.py, 3 files.")


def test_rename_refuses_a_node_without_name_sites(tools):
    # The fixture graph has no `name` metadata, like a graph from before v0.9.93-blockether.4.
    with pytest.raises(ValueError, match="grammar|not a code symbol"):
        tools.rename("connect", "open")
    with pytest.raises(ValueError, match="grammar|not a code symbol"):
        tools.references("app.core")


def test_add_parameter_states(tools):
    node = tools.explain("connect").node
    edit = CallEdit("src/app/db.clj", 9, "call", " opts")
    preview = SignatureResult(node, "opts", "{}", False, ("src/app/db.clj",), (edit,), (), "-a\n+b\n")
    assert summary(presentation.add_parameter(preview)) == "connect() + opts: preview, 1 call in 1 file"
    done = dataclasses.replace(preview, applied=True, check="go vet ./...: passed")
    assert summary(presentation.add_parameter(done)) == "connect() + opts: written, 1 call in 1 file, go vet passed"
    failed = dataclasses.replace(done, check="go vet ./...: failed\n./a.go:3:2: undefined: x")
    shown = presentation.add_parameter(failed)
    assert summary(shown) == "connect() + opts: written, 1 call in 1 file, go vet failed"
    assert "undefined: x" in shown.content[0].text
    blocked = SignatureResult(node, "opts", "{}", False, (), (), ("x: used as a value.",), "")
    assert summary(presentation.add_parameter(blocked)) == "connect() + opts: not written, 1 conflict"


def test_moves_show_the_knowledge_base():
    told = ("graphify-out/kb: 1 moved, 0 pending, 0 with no records at the old id",)
    node = Node("app_util_slugify", "slugify()", "app/util.py", 4, "function", None)
    renamed = RenameResult(node, "slugify", "slug", True, ("app/util.py",), (), (), (), 0, "", kb=told)
    files = ("app/main.py", "app/text.py", "app/util.py")
    moved = MoveResult(
        "slugify", "app/util.py", "app/text.py", True, files, ("app/text.py",), (), (), (), (), (), 0, "", kb=told
    )
    for shown in (presentation.rename(renamed), presentation.move(moved), presentation.move_file(moved)):
        assert "Knowledge base:\n" + told[0] in [getattr(block, "text", "") for block in shown.content]
    assert str(moved).endswith("\nKnowledge base " + told[0]) and str(renamed).endswith("\nKnowledge base " + told[0])
    quiet = presentation.move(dataclasses.replace(moved, kb=()))
    assert not any(getattr(block, "text", "").startswith("Knowledge base") for block in quiet.content)
