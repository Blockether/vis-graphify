import subprocess
from pathlib import Path

import pytest

from vis_graphify import graph as g
from vis_graphify.tools import GraphifyTools, graphify_command


def tools_for(root, runner=None):
    return GraphifyTools(workspace_root=lambda: str(root), runner=runner)


class FakeRunner:
    def __init__(self, returncode=0, stdout="", stderr="", effect=None):
        self.calls = []
        self.result = (returncode, stdout, stderr)
        self.effect = effect

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.effect:
            self.effect()
        code, out, err = self.result
        return subprocess.CompletedProcess(argv, code, out, err)


def test_build_runs_graphify_update_and_counts(project):
    runner = FakeRunner(stdout="[graphify update] done\n")
    result = tools_for(project, runner).build(force=True, cluster=False)
    argv, kwargs = runner.calls[0]
    assert argv[-4:] == ["update", str(project.resolve()), "--force", "--no-cluster"]
    assert kwargs["cwd"] == str(project.resolve())
    assert "PYTHONPATH" not in kwargs["env"]
    assert (result.ok, result.nodes, result.edges) == (True, 6, 4)
    assert "done" in result.output


def test_build_failure_raises_with_output(project):
    with pytest.raises(RuntimeError, match="status 2") as error:
        tools_for(project, FakeRunner(2, "", "boom")).build()
    assert "boom" in str(error.value)


def test_build_timeout(project):
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(TimeoutError):
        tools_for(project, runner).build(timeout_s=1)


def test_relative_root_uses_workspace_and_rejects_files(project):
    (project / "file.txt").write_text("x")
    with pytest.raises(NotADirectoryError):
        tools_for(project).status("file.txt")


def test_status_with_and_without_graph(project, tmp_path_factory):
    status = tools_for(project).status()
    assert (status.exists, status.nodes, status.edges, status.files) == (True, 6, 4, 4)
    assert status.relations == {"calls": 3, "contains": 1}
    empty = tools_for(tmp_path_factory.mktemp("empty")).status()
    assert (empty.exists, empty.nodes) == (False, 0)


def test_search_explain_path_hubs_affected(project):
    tools = tools_for(project)
    assert [n.id for n in tools.search("db").nodes][0].startswith("src_app_db")
    explained = tools.explain("app.core/run")
    assert explained.node.kind == "var" and explained.node.line == 5
    assert [e.node.label for e in explained.outgoing] == ["query()"]
    assert {e.relation for e in explained.incoming} == {"contains", "calls"}
    path = tools.shortest_path("app.web/run", "connect")
    assert path.found and [s.node.label for s in path.steps] == ["run()", "query()", "connect()"]
    assert tools.hubs(limit=1)[0].node.id == "src_app_core_run"
    impact = tools.affected("connect", depth=3)
    assert impact.files == ("src/app/core.clj", "src/app/db.clj", "src/app/web.clj")
    assert [i.distance for i in impact.impacts] == [1, 2, 3]


def test_explain_limit_truncates(project):
    assert tools_for(project).explain("app.core/run", limit=1).truncated


def test_query_passes_graph_and_budget(project):
    runner = FakeRunner(stdout="NODE run()\nEDGE run() --calls--> query()\n")
    answer = tools_for(project, runner).query("what does run call", budget=500)
    argv, _ = runner.calls[0]
    assert argv[-6:] == [
        "query",
        "what does run call",
        "--budget",
        "500",
        "--graph",
        str(project.resolve() / "graphify-out/graph.json"),
    ]
    assert answer.text.startswith("NODE")


def test_query_without_graph_raises(tmp_path):
    with pytest.raises(g.GraphMissing):
        tools_for(tmp_path, FakeRunner()).query("x")


def test_graphify_command_finds_an_interpreter():
    argv = graphify_command()
    assert Path(argv[0]).exists() or argv[0] == "graphify"
