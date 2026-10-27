"""Build a real graph of a small Clojure project with the pinned graphify fork."""

import os
import subprocess
import textwrap

import pytest

from vis_graphify import graph as g
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
    (src / "other.clj").write_text("(ns demo.other)\n\n;; connect is local here\n(defn connect [] :other)\n")
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    built = tools.build(cluster=False, timeout_s=600)
    assert built.nodes >= 5
    run = tools.explain("demo.core/run")
    assert {e.node.label for e in run.outgoing if e.relation == "calls"} >= {"connect()", "query()"}
    impact = tools.affected("demo.db/connect")
    assert "src/demo/core.clj" in impact.files
    assert impact.others == ("connect() src/demo/other.clj:4 [src_demo_other_connect], not used",)
    assert impact.functions == ("run",)
    assert str(impact).endswith("not used\nDependent functions, complete: run")
    assert tools.affected("demo.db/connect", within="src/demo/core.clj").files == ("src/demo/core.clj",)
    assert tools.affected("demo.db/connect", within="lib/").impacts == ()
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
    assert tools.build(cluster=False, timeout_s=600).nodes
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
    assert done.text_mentions >= 1 and not any(m.endswith(":connect") for m in done.mentions)


def _git_project(tmp_path):
    """`_rename_project` in a git work tree, where `references` can skip the graph update."""
    tools, src = _rename_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tools, src, tmp_path / "graphify-out" / "graph.json"


def _edit(path, graph, text):
    """Write `text` to `path` with a time after the build of `graph`."""
    path.write_text(text)
    os.utime(path, ns=(graph.stat().st_mtime_ns + 10**9,) * 2)


def test_real_references_read_clojure_edits_without_a_graph_update(tmp_path):
    # Before this fix, each call after an edit ran a full `graphify update`: 65-98 s on the Vis repository.
    tools, src, graph = _git_project(tmp_path)
    built = graph.stat().st_mtime_ns
    _edit(src / "db.clj", graph, "(ns demo.db)\n\n;; moved down\n\n(defn connect [url] {:url url})\n")
    _edit(src / "core.clj", graph, (src / "core.clj").read_text() + "\n(defn again [u] (db/connect u))\n")
    found = tools.references("demo.db/connect")
    short = tools.references("connect()")
    assert graph.stat().st_mtime_ns == built
    assert found.node.line == 5
    assert ("src/demo/core.clj", 9, "use") in [(ref.file, ref.line, ref.role) for ref in found.references]
    tools.build(cluster=False, timeout_s=600)
    assert found == short == tools.references("demo.db/connect")


def test_real_references_update_the_graph_for_a_new_file_or_a_rival_name(tmp_path):
    tools, src, graph = _git_project(tmp_path)
    built = graph.stat().st_mtime_ns
    _edit(src / "extra.clj", graph, "(ns demo.extra\n  (:require [demo.db :as db]))\n\n(defn go [] (db/connect 1))\n")
    assert "src/demo/extra.clj" in tools.references("demo.db/connect").files
    assert graph.stat().st_mtime_ns > built
    _edit(src / "core.clj", graph, (src / "core.clj").read_text() + "\n(defn connect [] :mine)\n")
    with pytest.raises(g.AmbiguousNode):
        tools.references("connect()")


def test_real_references_update_the_graph_for_a_removed_definition(tmp_path):
    tools, src, graph = _git_project(tmp_path)
    _edit(src / "db.clj", graph, "(ns demo.db)\n\n(defn open [url] {:url url})\n")
    with pytest.raises(g.NodeNotFound):
        tools.references("demo.db/connect")


def test_real_references_update_the_graph_after_an_edit_outside_clojure(tmp_path):
    (tmp_path / "util.py").write_text("def helper(x):\n    return x\n")
    (tmp_path / "main.py").write_text("from util import helper\n")
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    assert tools.build(cluster=False, timeout_s=600).nodes
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    graph = tmp_path / "graphify-out" / "graph.json"
    built = graph.stat().st_mtime_ns
    _edit(tmp_path / "main.py", graph, "from util import helper\n\nhelper(1)\n")
    assert ("main.py", 3, "use") in [(ref.file, ref.line, ref.role) for ref in tools.references("helper()").references]
    assert graph.stat().st_mtime_ns > built


def test_real_rename_conflict_writes_nothing(tmp_path):
    tools, src = _rename_project(tmp_path)
    before = (src / "core.clj").read_text()
    blocked = tools.rename("demo.db/connect", "run", apply=True)
    assert not blocked.applied and any("already uses `run`" in c for c in blocked.conflicts)
    assert (src / "core.clj").read_text() == before


def test_real_rename_across_python_and_typescript(tmp_path):
    files = {
        "py/util.py": "def helper(x):\n    return x\n",
        "py/main.py": "from util import helper\n\n\ndef run():\n    # helper is the identity\n    return helper(1)\n",
        "web/util.ts": "export function formatName(n: string): string {\n  return n.trim();\n}\n",
        "web/main.ts": "import { formatName } from './util';\n\nexport const a = formatName(' x ');\n",
    }
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(body, encoding="utf-8")
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    assert tools.build(cluster=False, timeout_s=600).nodes
    found = tools.references("helper()")
    assert sorted((r.file, r.role) for r in found.references) == [
        ("py/main.py", "import"),
        ("py/main.py", "use"),
        ("py/util.py", "definition"),
    ]
    preview = tools.rename("helper()", "assist")
    assert not preview.applied and not preview.conflicts
    assert (preview.mentions, preview.text_mentions) == ((), 1)
    assert "+def assist(x):" in preview.diff
    renamed = tools.rename("helper()", "assist", apply=True)
    assert renamed.applied and renamed.check == "python syntax of 2 files: passed"
    assert "No other code spells helper." in str(renamed) and str(renamed).endswith(
        "Check python syntax of 2 files: passed"
    )
    assert "return assist(1)" in (tmp_path / "py/main.py").read_text(encoding="utf-8")
    done = tools.rename("formatName()", "displayName", apply=True)
    assert done.applied and done.files == ("web/main.ts", "web/util.ts")
    assert "import { displayName } from './util';" in (tmp_path / "web/main.ts").read_text(encoding="utf-8")


def test_real_add_parameter_across_languages(tmp_path):
    files = {
        "py/util.py": "def helper(x):\n    return x\n",
        "py/main.py": "from util import helper\n\n\ndef run():\n    return helper(1) + helper(2)\n",
        "web/util.ts": "export function formatName(n: string): string {\n  return n.trim();\n}\n",
        "web/main.ts": "import { formatName } from './util';\n\nexport const a = formatName(' x ');\n",
        "src/demo/db.clj": "(ns demo.db)\n\n(defn connect [url] {:url url})\n",
        "src/demo/core.clj": "(ns demo.core\n  (:require [demo.db :as db]))\n\n(defn run [u]\n  (db/connect u))\n",
    }
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(body, encoding="utf-8")
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    preview = tools.add_parameter("helper()", "scale=1", "scale=3")
    assert not preview.applied and not preview.conflicts
    assert "+    return helper(1, scale=3) + helper(2, scale=3)" in preview.diff
    assert "helper(1)" in (tmp_path / "py/main.py").read_text(encoding="utf-8")  # a preview writes nothing
    added = tools.add_parameter("helper()", "scale=1", "scale=3", apply=True)
    assert added.applied and added.check == "python syntax of 2 files: passed"
    assert "def helper(x, scale=1):" in (tmp_path / "py/util.py").read_text(encoding="utf-8")
    done = tools.add_parameter("formatName()", "upper: boolean", "true", apply=True)
    assert done.applied and done.files == ("web/main.ts", "web/util.ts")
    assert "web/main.ts:3  export const a = formatName(' x ', true);" in str(done)
    assert "formatName(' x ', true)" in (tmp_path / "web/main.ts").read_text(encoding="utf-8")
    assert tools.add_parameter("demo.db/connect", "opts", "{}", apply=True).applied
    assert "(defn connect [url opts]" in (tmp_path / "src/demo/db.clj").read_text(encoding="utf-8")
    assert "(db/connect u {})" in (tmp_path / "src/demo/core.clj").read_text(encoding="utf-8")


def test_real_add_parameter_conflict_writes_nothing(tmp_path):
    (tmp_path / "jobs.py").write_text("def job(a, b=1):\n    return a\n\n\nhandlers = [job]\n")
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    blocked = tools.add_parameter("job()", "c", "0", apply=True)
    assert not blocked.applied and len(blocked.conflicts) == 2
    assert "Nothing was written" in str(blocked)
    assert (tmp_path / "jobs.py").read_text().startswith("def job(a, b=1):")


def _write(root, files):
    for name, body in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body, encoding="utf-8")


def _note(root, node_id):
    """Link a knowledge base document of the project store to `node_id`."""
    from graphify.kb import KnowledgeBase

    knowledge = KnowledgeBase(root / "graphify-out" / "graph.json")
    note = {"op": "add_document", "title": "Notes", "body": "Uses the pool.", "nodes": [node_id]}
    knowledge.batch([note], actor="tester")
    knowledge.flush()


def _linked(root):
    """The node ids that the document of `_note` links."""
    from graphify.kb import KnowledgeBase

    return KnowledgeBase(root / "graphify-out" / "graph.json", create=False).get_document("notes")["nodes"]


def test_real_move_definition_in_python_and_typescript(tmp_path):
    _write(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/util.py": "import re\n\n\ndef slugify(s):\n    return re.sub(r'\\W+', '-', s)\n\n\ndef keep():\n    return 1\n",
            "app/main.py": "from app.util import keep, slugify\n\n\ndef run():\n    return slugify('a b'), keep()\n",
            "web/util.ts": "export function formatName(n: string): string {\n  return n.trim();\n}\n",
            "web/main.ts": "import { formatName } from './util';\n\nexport const a = formatName(' x ');\n",
        },
    )
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    assert tools.build(cluster=False, timeout_s=600).nodes
    _note(tmp_path, "app_util_slugify")
    preview = tools.move("app/util.py::slugify", "app/text.py")
    assert not preview.applied and not preview.conflicts and preview.created == ("app/text.py",)
    assert "--- /dev/null" in preview.diff and not (tmp_path / "app/text.py").exists()  # a preview writes nothing
    done = tools.move("app/util.py::slugify", "app/text.py", apply=True)
    assert done.applied and done.check.endswith(": passed")
    text = (tmp_path / "app/text.py").read_text(encoding="utf-8")
    assert "import re" in text and "def slugify(s):" in text
    assert "def slugify" not in (tmp_path / "app/util.py").read_text(encoding="utf-8")
    assert "from app.text import slugify" in (tmp_path / "app/main.py").read_text(encoding="utf-8")
    # The knowledge base document follows the new node id.
    assert done.moved_nodes == ("app_util_slugify -> app_text_slugify",) and _linked(tmp_path) == ["app_text_slugify"]
    assert done.kb == ("graphify-out/kb: 1 moved, 0 pending, 0 with no records at the old id",)
    moved = tools.move("formatName()", "web/names.ts", apply=True)
    assert moved.applied and "web/names.ts" in moved.created
    assert "from './names'" in (tmp_path / "web/main.ts").read_text(encoding="utf-8")


def test_real_move_file_moves_the_clojure_test_namespace(tmp_path):
    _write(
        tmp_path,
        {
            "src/app/util.clj": '(ns app.util)\n\n(defn shout [s] (str s "!"))\n',
            "src/app/core.clj": '(ns app.core\n  (:require [app.util :as util]))\n\n(defn run [] (util/shout "hi"))\n',
            "test/app/util_test.clj": (
                '(ns app.util-test\n  (:require [app.util :as util]))\n\n(defn check [] (util/shout "t"))\n'
            ),
        },
    )
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    assert tools.build(cluster=False, timeout_s=600).nodes
    done = tools.move_file("src/app/util.clj", "src/app/text/core.clj", apply=True, check=False)
    assert done.applied and "app.util-test -> app.text.core-test" in done.renames
    assert set(done.deleted) == {"src/app/util.clj", "test/app/util_test.clj"}
    assert (tmp_path / "src/app/text/core.clj").read_text(encoding="utf-8").startswith("(ns app.text.core)")
    tested = (tmp_path / "test/app/text/core_test.clj").read_text(encoding="utf-8")
    assert tested.startswith("(ns app.text.core-test\n  (:require [app.text.core :as util]))")
    assert "[app.text.core :as util]" in (tmp_path / "src/app/core.clj").read_text(encoding="utf-8")


def test_real_move_conflict_writes_nothing(tmp_path):
    _write(tmp_path, {"a.py": "def job():\n    return 1\n", "b.py": "def job():\n    return 2\n"})
    tools = GraphifyTools(workspace_root=lambda: str(tmp_path))
    assert tools.build(cluster=False, timeout_s=600).nodes
    blocked = tools.move("a.py::job", "b.py", apply=True)
    assert not blocked.applied and blocked.conflicts and "Nothing was written" in str(blocked)
    assert (tmp_path / "a.py").read_text() == "def job():\n    return 1\n"


def test_real_rename_and_move_file_keep_knowledge_base_links(tmp_path):
    tools, _ = _rename_project(tmp_path)
    _note(tmp_path, "src_demo_db_connect")
    renamed = tools.rename("demo.db/connect", "open-conn", apply=True, check=False)
    assert renamed.moved_nodes == ("src_demo_db_connect -> src_demo_db_open_conn",)
    assert renamed.kb == ("graphify-out/kb: 1 moved, 0 pending, 0 with no records at the old id",)
    assert _linked(tmp_path) == ["src_demo_db_open_conn"]
    moved = tools.move_file("src/demo/db.clj", "src/demo/store.clj", apply=True, check=False)
    assert "src_demo_db_open_conn -> src_demo_store_open_conn" in moved.moved_nodes and moved.kb
    assert _linked(tmp_path) == ["src_demo_store_open_conn"]
