import importlib.util
from pathlib import Path

import blockether.vis.extension as vis
import pytest

from vis_graphify import presentation
from vis_graphify.tools import (
    BuildResult,
    DomainFinding,
    DomainReport,
    Explanation,
    GraphifyTools,
    ImpactResult,
    PathResult,
    Reference,
    ReferencesResult,
    RenameEdit,
    RenameResult,
    SearchResult,
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
    assert summary(presentation.affected(tools.affected("connect", depth=3))) == "3 dependents in 3 files"
    build = BuildResult(str(project), "g.json", True, 0, 6, 4, 1.5, "done")
    assert summary(presentation.build(build)) == "6 nodes, 4 edges in 1.5 s"


def test_empty_states(tools, tmp_path_factory):
    empty = GraphifyTools(workspace_root=lambda: str(tmp_path_factory.mktemp("empty")))
    assert summary(presentation.status(empty.status())) == "no graph yet"
    assert summary(presentation.search(SearchResult("zzz", 0, ()))) == "no matches"
    assert summary(presentation.hubs(())) == "no edges"
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


def test_extension_registers_every_tool_with_an_activity(monkeypatch):
    registered = []
    monkeypatch.setattr(vis, "register_extension", registered.append)
    spec = importlib.util.spec_from_file_location("vis_graphify_entry", ROOT / "extension.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
            "domain",
        ]
    )
    for name in public:
        method = getattr(GraphifyTools, name)
        assert method.__doc__, name


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
        node, "connect", "open", False, ("src/app/db.clj",), (edit,), (), ("src/app/db.clj:2 :connect",), "-a\n+b\n"
    )
    assert summary(presentation.rename(preview)) == "connect → open: preview, 1 edit in 1 file"
    done = RenameResult(node, "connect", "open", True, ("src/app/db.clj",), (edit,), (), (), "-a\n+b\n")
    assert summary(presentation.rename(done)) == "connect → open: renamed, 1 edit in 1 file"
    blocked = RenameResult(node, "connect", "run", False, (), (), ("x already uses `run`.",), (), "")
    assert summary(presentation.rename(blocked)) == "connect → run: not written, 1 conflict"


def test_rename_refuses_a_node_without_name_sites(tools):
    # The fixture graph has no `name` metadata, like a graph from before v0.9.93-blockether.4.
    with pytest.raises(ValueError, match="grammar|not a code symbol"):
        tools.rename("connect", "open")
    with pytest.raises(ValueError, match="grammar|not a code symbol"):
        tools.references("app.core")
