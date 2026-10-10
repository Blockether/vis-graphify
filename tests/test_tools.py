import json
import os
import subprocess
from pathlib import Path

import pytest

from vis_graphify import graph as g
from vis_graphify.tools import BUNDLED_PLUGINS, GraphifyTools, graphify_command


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


DOMAIN_JSON = {
    "root": "/p",
    "markdown": "/p/graphify-out/domain/DOMAIN.md",
    "json": "/p/graphify-out/domain/domain.json",
    "plugins": ["edd-core"],
    "services": ["inventory-svc", "orders-svc"],
    "counts": {"command": 2, "event": 2},
    "relations": 4,
    "schemas": 1,
    "findings": [
        {
            "severity": "error",
            "code": "missing-command",
            "message": "on-placed triggers `release-stok`.",
            "file": "orders-svc/src/orders/main.clj",
            "line": 17,
            "ref": "command:inventory-svc:release-stok",
            "plugin": "edd-core",
        },
        {"severity": "info", "code": "query-unused", "message": "x", "file": None, "line": None, "ref": None},
    ],
    "finding_counts": {"error": 1, "warning": 0, "info": 1},
}


def test_domain_runs_plugins_and_parses_the_report(project):
    runner = FakeRunner(stdout=json.dumps(DOMAIN_JSON), stderr="warning: plugin error: bad.py: boom\n")
    report = tools_for(project, runner).domain(plugins=["edd-core", "bad.py"], limit=1)
    argv, kwargs = runner.calls[0]
    assert argv[-7:] == ["domain", str(project.resolve()), "--json", "--plugin", "edd-core", "--plugin", "bad.py"]
    assert kwargs["env"]["GRAPHIFY_PLUGINS"].split(os.pathsep)[0] == str(BUNDLED_PLUGINS)
    assert (report.plugins, report.services) == (("edd-core",), ("inventory-svc", "orders-svc"))
    assert (report.errors, report.warnings, report.notes, report.relations) == (1, 0, 1, 4)
    assert report.markdown_path.endswith("DOMAIN.md") and report.counts == {"command": 2, "event": 2}
    (finding,) = report.findings
    assert (finding.code, finding.line, finding.element) == (
        "missing-command",
        17,
        "command:inventory-svc:release-stok",
    )
    assert report.truncated and report.plugin_errors == ("bad.py: boom",)


def test_domain_without_findings_limit_keeps_optional_fields(project):
    report = tools_for(project, FakeRunner(stdout=json.dumps(DOMAIN_JSON))).domain()
    note = report.findings[1]
    assert (note.file, note.line, note.element, note.plugin) == ("", None, "", "")
    assert not report.truncated


def test_domain_failure_and_timeout_raise(project):
    with pytest.raises(RuntimeError, match="status 2") as error:
        tools_for(project, FakeRunner(2, "", "error: x.py: file not found")).domain(plugins=["x.py"])
    assert "file not found" in str(error.value)
    with pytest.raises(RuntimeError, match="status 0"):
        tools_for(project, FakeRunner(0, "not json")).domain()

    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    with pytest.raises(TimeoutError):
        tools_for(project, runner).domain(timeout_s=1)


def test_domain_plugins_lists_rows(project):
    rows = [{"name": "edd-core", "description": "edd", "module": "graphify.plugins.edd_core"}]
    runner = FakeRunner(stdout=json.dumps(rows))
    (plugin,) = tools_for(project, runner).domain_plugins()
    assert runner.calls[0][0][-2:] == ["--list-plugins", "--json"]
    assert (plugin.name, plugin.module) == ("edd-core", "graphify.plugins.edd_core")
    with pytest.raises(RuntimeError):
        tools_for(project, FakeRunner(1, "", "boom")).domain_plugins()


def test_build_passes_plugins_to_graphify(project, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PLUGINS", "user.py")
    runner = FakeRunner()
    tools_for(project, runner).build(plugins=["routes.py"])
    env = runner.calls[0][1]["env"]
    assert env["GRAPHIFY_PLUGINS"].split(os.pathsep) == [str(BUNDLED_PLUGINS), "routes.py", "user.py"]
