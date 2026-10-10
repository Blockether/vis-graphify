"""Build a real graph of a small Clojure project with the pinned graphify fork."""

import pytest

from vis_graphify.tools import GraphifyTools

pytest.importorskip("graphify")


def test_real_clojure_graph(tmp_path):
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "db.clj").write_text(
        "(ns demo.db)\n\n(defn connect [url] {:url url})\n\n(defn query [conn q]\n  (assoc conn :q q))\n"
    )
    (src / "core.clj").write_text(
        "(ns demo.core\n  (:require [demo.db :as db]))\n\n"
        '(defn run [url]\n  (let [conn (db/connect url)]\n    (db/query conn "select 1")))\n'
    )
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    built = tools.build(cluster=False, timeout_s=600)
    assert built.ok and built.nodes >= 5
    run = tools.explain("demo.core/run")
    assert {e.node.label for e in run.outgoing if e.relation == "calls"} >= {"connect()", "query()"}
    impact = tools.affected("demo.db/connect")
    assert "src/demo/core.clj" in impact.files
    assert tools.shortest_path("demo.core/run", "demo.db/query").found


def test_real_graph_links_imports_and_test_redefs(tmp_path):
    # Fixed in graphify v0.9.84-blockether.2: shared requires reach the real
    # namespace node, and with-redefs targets count as dependents.
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "db.clj").write_text("(ns demo.db)\n\n(defn connect [url] {:url url})\n")
    for name in ("a", "b"):
        (src / f"{name}.clj").write_text(
            f"(ns demo.{name}\n  (:require [demo.db :as db]))\n\n"
            f"(defn t-{name} []\n  (with-redefs [db/connect (constantly nil)] 1))\n"
        )
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    tools.build(cluster=False, timeout_s=600)
    namespace = tools.explain("demo.db")
    assert namespace.node.kind == "namespace"
    assert {e.relation for e in namespace.incoming} >= {"imports"}
    assert tools.affected("demo.db/connect").files == ("src/demo/a.clj", "src/demo/b.clj")
