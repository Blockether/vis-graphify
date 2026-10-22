import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from vis_graphify.tools import BUNDLED_PLUGINS, GraphifyTools, Node, RenameEdit, RenameResult, graphify_command


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
    assert (result.nodes, result.edges) == (6, 4)
    assert str(result) == "Graph built: 6 nodes, 4 edges in %g s." % result.seconds
    assert (project / "graphify-out" / ".gitignore").read_text() == "*\n"


def test_rename_text_is_short():
    node = Node("n", "checksum()", "util.rs", 1, "function", None)
    edits = tuple(RenameEdit(f"f{i}.rs", 1, 1, "checksum", "crc32") for i in range(3))
    diff = "\n".join(f"+line {i}" for i in range(100))
    mentions = tuple(f"misc/r{i}.rs:{i} checksum (another symbol)" for i in range(12))
    preview = RenameResult(node, "checksum", "crc32", False, ("f0.rs",), edits, (), mentions, 500, diff)
    text = str(preview)
    assert text.startswith("Preview checksum -> crc32: 3 edits in 1 files. Call again with apply=True to write:")
    assert "… 40 more lines in `diff`." in text and "… 2 more in `mentions`." in text
    assert "Kept 500 mentions in strings, comments and keywords." in text
    assert len(text) < 1500
    blocked = dataclasses.replace(preview, conflicts=("f0.rs already uses crc32",))
    assert str(blocked).splitlines()[-1] == "f0.rs already uses crc32"
    applied = dataclasses.replace(preview, applied=True)
    assert str(applied).splitlines()[:2] == ["Renamed checksum -> crc32: 3 edits in 1 files:", "+line 0"]
    changed = dataclasses.replace(applied, changes=tuple(f"f{i}.rs:1  crc32()" for i in range(90)))
    assert str(changed).splitlines()[1:3] == ["f0.rs:1  crc32()", "f1.rs:1  crc32()"]
    assert "… 10 more in `changes`." in str(changed) and "+line" not in str(changed)


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
    assert tools.hubs(limit=1).hubs[0].node.id == "src_app_core_run"
    impact = tools.affected("connect", depth=3)
    assert impact.files == ("src/app/core.clj", "src/app/db.clj", "src/app/web.clj")
    assert [i.distance for i in impact.impacts] == [1, 2, 3]
    assert tools.affected("connect").impacts == impact.impacts and tools.affected("connect").depth == 3
    assert tools.affected("connect", depth=1).files == ("src/app/db.clj",)


def test_result_text_is_one_line_per_row(project):
    tools = tools_for(project)
    assert str(tools.search("db")).splitlines()[0] == "2 of 2 nodes match 'db':"
    assert str(tools.affected("connect")).splitlines()[1].startswith("  1  calls      query()  src/app/db.clj:")
    assert str(tools.shortest_path("app.web/run", "connect")).endswith("-> connect()")
    assert str(tools.hubs(limit=1)).splitlines()[1].endswith("[src_app_core_run]")
    explained = str(tools.explain("app.core/run")).splitlines()
    assert explained[1] == "Uses:" and "Used by:" in explained


def test_queries_build_a_missing_graph_and_update_a_stale_one(project):
    runner = FakeRunner()
    tools = tools_for(project, runner)
    graph = project / "graphify-out" / "graph.json"
    source = project / "src" / "app" / "db.clj"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("(ns app.db)\n")
    os.utime(graph, ns=(source.stat().st_mtime_ns + 10**9,) * 2)
    tools.search("db")
    assert runner.calls == []
    os.utime(source, ns=(graph.stat().st_mtime_ns + 10**9,) * 2)
    tools.search("db")
    assert runner.calls[-1][0][-2] == "update"


def test_explain_limit_truncates(project):
    assert tools_for(project).explain("app.core/run", limit=1).truncated


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


def test_build_passes_plugins_to_graphify(project, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_PLUGINS", "user.py")
    runner = FakeRunner()
    tools_for(project, runner).build(plugins=["routes.py"])
    env = runner.calls[0][1]["env"]
    assert env["GRAPHIFY_PLUGINS"].split(os.pathsep) == [str(BUNDLED_PLUGINS), "routes.py", "user.py"]
