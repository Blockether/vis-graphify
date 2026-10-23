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
    Explanation,
    GraphifyTools,
    HubsResult,
    ImpactResult,
    MoveResult,
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
    render = presentation.renderer("Explain code node", presentation.explain, ("node", "root"))
    failed = render(phase="failure", args=(object(), "run"), kwargs={"root": "."}, error=ValueError("ambiguous"))
    assert failed.summary == "failed"
    texts = [getattr(block, "text", "") for block in failed.content]
    assert "node='run', root='.'" in texts and "ambiguous" in texts
    started = render(phase="start", args=("run",), kwargs={})
    assert started.summary == "running"
    assert render(phase="start", args=(), kwargs={}) is None
    assert render(phase="success", result=None) is None


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
            "kb_serve",
            "kb_stop",
            "kb_connect",
            "kb_search",
            "kb_node",
            "kb_write",
            "kb_ingest",
            "kb_validate",
            "kb_undo",
            "kb_publish",
        ]
    )
    for name in public:
        method = getattr(GraphifyTools, name)
        assert method.__doc__, name


def test_extension_reads_the_kb_token_in_a_masked_form(monkeypatch):
    module, _ = _entry(monkeypatch)
    vault, forgotten, asked = {"vis-secret:1": "kb-token-0123456789"}, [], []
    monkeypatch.setattr(vis, "reveal", vault.get)
    monkeypatch.setattr(vis, "forget", forgotten.append)
    assert module._kb_secret("kb-token-plain-0123") == "kb-token-plain-0123"
    assert module._kb_secret("vis-secret:1") == "kb-token-0123456789"
    with pytest.raises(ValueError, match="unknown or forgotten"):
        module._kb_secret("vis-secret:2")

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
