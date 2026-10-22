"""Build a real graph of a small Clojure project with the pinned graphify fork."""

import textwrap

import pytest

from vis_graphify import tools as tools_module
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


ORDERS = """(ns orders.main
  (:require [edd.core :as edd]))

(def PlaceOrder
  [:map
   [:sku string?]
   [:qty pos-int?]])

(defn place-order [_ctx cmd]
  {:event-id :order-placed
   :sku (:sku cmd)})

(defn on-placed [_ctx event]
  {:service :inventory-svc
   :commands [{:cmd-id :reserve-stock
               :id (:id event)}
              {:cmd-id :release-stok
               :id (:id event)}]})

(defn register [ctx]
  (-> ctx
      (edd/reg-cmd :place-order place-order :consumes PlaceOrder)
      (edd/reg-event :order-placed (fn [agg _] agg))
      (edd/reg-fx :order-placed on-placed)))
"""

INVENTORY = """(ns inventory.main
  (:require [edd.core :as edd]))

(defn reserve-stock [_ctx cmd]
  {:event-id :stock-reserved
   :id (:id cmd)})

(defn register [ctx]
  (-> ctx
      (edd/reg-cmd :reserve-stock reserve-stock)
      (edd/reg-event :stock-reserved (fn [agg _] agg))))
"""

JOBS_PLUGIN = textwrap.dedent("""
    from graphify.domain import edn
    from graphify.domain.model import Element, KindSpec, Ref
    from graphify.domain.plugin import DomainPlugin


    class JobsPlugin(DomainPlugin):
        name = "jobs"
        description = "Reads (defjob :name ...)."
        kinds = {"job": KindSpec("Job", "Jobs", "stadium", entry=True)}

        def extract(self, corpus, model):
            for sf in corpus.files:
                for top in sf.forms:
                    if top.kind == edn.LIST and top.head is not None and top.head.is_sym("defjob"):
                        model.add(Element(Ref("job", "jobs", top.items[1].name), sf.rel, top.line, plugin=self.name))


    plugin = JobsPlugin
""")


def _shop(root):
    for rel, text in {
        "orders-svc/src/orders/main.clj": ORDERS,
        "inventory-svc/src/inventory/main.clj": INVENTORY,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return GraphifyTools(workspace_root=lambda: str(root))


def test_real_edd_core_domain_report(tmp_path):
    tools = _shop(tmp_path)
    report = tools.domain()
    assert report.plugins == ("edd-core",)
    assert report.services == ("inventory-svc", "orders-svc")
    assert report.counts == {"command": 2, "effect": 1, "event": 2}
    assert report.errors == 1 and report.schemas >= 1
    (missing,) = [f for f in report.findings if f.severity == "error"]
    assert (missing.code, missing.element) == ("missing-command", "command:inventory-svc:release-stok")
    markdown = (tmp_path / "graphify-out/domain/DOMAIN.md").read_text()
    assert "```mermaid" in markdown and "release-stok" in markdown
    assert list((tmp_path / "graphify-out/domain/schemas").rglob("*.json"))


def test_real_build_adds_domain_nodes(tmp_path):
    tools = _shop(tmp_path)
    tools.build(cluster=False, timeout_s=600)
    command = tools.explain("domain:command:orders-svc:place-order")
    assert {(e.relation, e.node.label) for e in command.outgoing} >= {("handled_by", "place-order()")}


def test_project_and_bundled_plugins(tmp_path, monkeypatch):
    tools = _shop(tmp_path)
    (tmp_path / "jobs-svc/src/jobs").mkdir(parents=True)
    (tmp_path / "jobs-svc/src/jobs/core.clj").write_text("(ns jobs.core)\n\n(defjob :nightly-report)\n")
    (tmp_path / ".graphify/plugins").mkdir(parents=True)
    (tmp_path / ".graphify/plugins/jobs.py").write_text(JOBS_PLUGIN)
    report = tools.domain()
    assert set(report.plugins) == {"edd-core", "jobs"} and report.counts["job"] == 1
    only = tools.domain(plugins=["jobs"])
    assert only.plugins == ("jobs",) and only.counts == {"job": 1}

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "batch.py").write_text(JOBS_PLUGIN.replace('name = "jobs"', 'name = "batch"'))
    monkeypatch.setattr(tools_module, "BUNDLED_PLUGINS", bundled)
    assert "batch" in {p.name for p in tools.domain_plugins()}
    assert tools.domain(plugins=["batch"]).counts == {"job": 1}


def test_real_schema_and_payload_checks(tmp_path):
    files = {
        "orders-svc/src/orders/main.clj": """(ns orders.main
  (:require [edd.core :as edd]))

(defn on-placed [_ctx event]
  {:service :inventory-svc
   :commands [{:cmd-id :reserve-stock
               :id (:id event)
               :attrs {:qty 0}}]})

(defn register [ctx]
  (-> ctx
      (edd/reg-event :order-placed (fn [agg _] agg))
      (edd/reg-fx :order-placed on-placed)))
""",
        "inventory-svc/src/inventory/main.clj": """(ns inventory.main
  (:require [edd.core :as edd]))

(defn reserve-stock [_ctx cmd]
  {:event-id :stock-reserved
   :id (:id cmd)})

(defn register [ctx]
  (-> ctx
      (edd/reg-cmd :reserve-stock reserve-stock
                   :consumes [:map [:attrs [:map [:qty pos-int?] [:sku {:optional true} :stirng]]]])
      (edd/reg-event :stock-reserved (fn [agg _] agg))))
""",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    report = GraphifyTools(workspace_root=lambda: str(tmp_path)).domain()
    errors = {f.code: f for f in report.findings if f.severity == "error"}
    assert set(errors) == {"payload-invalid", "schema-unknown-type"}
    assert errors["payload-invalid"].file == "orders-svc/src/orders/main.clj"
    assert "attrs.qty: is 0, expected at least 1" in errors["payload-invalid"].message
    assert "`:stirng`" in errors["schema-unknown-type"].message


def _rename_project(tmp_path):
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "db.clj").write_text("(ns demo.db)\n\n(defn connect [url] {:url url})\n")
    (src / "core.clj").write_text(
        "(ns demo.core\n  (:require [demo.db :as db :refer [connect]]))\n\n"
        "(defn run [url]\n  (db/connect (connect url)))\n\n"
        "(defn local [connect] (connect :connect))\n"
    )
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    assert tools.build(cluster=False, timeout_s=600).ok
    return tools, src


def test_real_references_and_rename(tmp_path):
    tools, src = _rename_project(tmp_path)
    found = tools.references("demo.db/connect")
    assert found.files == ("src/demo/core.clj", "src/demo/db.clj")
    assert sorted(ref.role for ref in found.references) == ["definition", "refer", "use", "use"]
    preview = tools.rename("demo.db/connect", "open-conn")
    assert not preview.applied and preview.conflicts == ()
    assert "-  (db/connect (connect url)))" in preview.diff and "+  (db/open-conn (open-conn url)))" in preview.diff
    assert "connect url" in (src / "core.clj").read_text()  # a preview writes nothing
    done = tools.rename("demo.db/connect", "open-conn", apply=True)
    assert done.applied and done.files == ("src/demo/core.clj", "src/demo/db.clj")
    core = (src / "core.clj").read_text()
    assert ":refer [open-conn]" in core and "(defn local [connect] (connect :connect))" in core
    assert "src/demo/core.clj:7 :connect" in done.mentions


def test_real_rename_conflict_writes_nothing(tmp_path):
    tools, src = _rename_project(tmp_path)
    before = (src / "core.clj").read_text()
    blocked = tools.rename("demo.db/connect", "run", apply=True)
    assert not blocked.applied and any("already uses `run`" in c for c in blocked.conflicts)
    assert (src / "core.clj").read_text() == before
