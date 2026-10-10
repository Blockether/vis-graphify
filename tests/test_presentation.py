import importlib.util
from pathlib import Path

import blockether.vis.extension as vis
import pytest

from vis_graphify import presentation
from vis_graphify.tools import (
    BuildResult,
    Explanation,
    GraphifyTools,
    ImpactResult,
    PathResult,
    QueryAnswer,
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
    answer = QueryAnswer("q", "NODE a\nNODE b\nEDGE a --calls--> b")
    assert summary(presentation.query(answer)) == "2 nodes, 1 edge"
    cut = QueryAnswer("q", "[!] TRUNCATED: showing 1 of 9 nodes\nNODE a")
    assert summary(presentation.query(cut)) == "1 node, 0 edges, truncated"


def test_empty_states(tools, tmp_path_factory):
    empty = GraphifyTools(workspace_root=lambda: str(tmp_path_factory.mktemp("empty")))
    assert summary(presentation.status(empty.status())) == "no graph yet"
    assert summary(presentation.search(SearchResult("zzz", 0, ()))) == "no matches"
    assert summary(presentation.hubs(())) == "no edges"
    lonely = tools.explain("lonely")
    assert summary(presentation.explain(Explanation(lonely.node, (), (), False))) == "lonely(): 0 outgoing, 0 incoming"
    assert summary(presentation.shortest_path(PathResult(lonely.node, lonely.node, False, ()))) == "not connected"
    assert summary(presentation.affected(ImpactResult(lonely.node, 2, (), (), False))) == "no dependents"
    assert summary(presentation.query(QueryAnswer("q", ""))) == "no answer"


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
        ["build", "status", "search", "explain", "shortest_path", "hubs", "affected", "query"]
    )
    for name in public:
        method = getattr(GraphifyTools, name)
        assert method.__doc__, name


def test_versions_agree():
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    text = (ROOT / "extension.py").read_text()
    assert f'VERSION = "{declared}"' in text
