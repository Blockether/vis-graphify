import json
import os

import pytest

from vis_graphify import graph as g


def test_load_reuses_unchanged_graph_and_reloads_changed(project):
    first = g.load(project)
    assert g.load(project) is first
    data = json.loads((project / "graphify-out/graph.json").read_text())
    data["nodes"].append({"id": "new", "label": "new()"})
    (project / "graphify-out/graph.json").write_text(json.dumps(data))
    assert "new" in g.load(project).nodes


def test_changes_finds_edited_added_and_deleted_files(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir()
    nodes = [{"id": f, "label": f, "source_file": f} for f in ("a.clj", "b.clj", "gone.clj")]
    (out / "graph.json").write_text(json.dumps({"nodes": nodes, "links": []}))
    built = (out / "graph.json").stat().st_mtime_ns
    for name, later in (("a.clj", True), ("b.clj", False), ("old.md", False), ("new.clj", True)):
        (tmp_path / name).write_text("x")
        os.utime(tmp_path / name, ns=(built + (10**9 if later else -(10**9)),) * 2)
    graph = g.load(tmp_path)
    listed = {"a.clj", "b.clj", "old.md", "new.clj", "gone.clj"}
    found = g.changes(graph, tmp_path, listed)
    assert found == g.Changes(frozenset({"a.clj"}), frozenset({"new.clj"}), frozenset({"gone.clj"}))
    assert found and not g.Changes(frozenset(), frozenset(), frozenset())
    assert g.changes(graph, tmp_path, listed - {"b.clj"}) is None  # git ignores a file of the graph


def test_load_without_graph_raises(tmp_path):
    with pytest.raises(g.GraphMissing, match="graphify.build"):
        g.load(tmp_path)


@pytest.mark.parametrize(
    "reference, expected",
    [
        ("src_app_db_query", "src_app_db_query"),
        ("query()", "src_app_db_query"),
        ("connect", "src_app_db_connect"),
        ("app.core/run", "src_app_core_run"),
        ("app.web/run", "src_app_web_run"),
        ("CONNE", "src_app_db_connect"),
        ("src/app/web.clj::run", "src_app_web_run"),
        ("app/core.clj:run()", "src_app_core_run"),
        ("web.clj#run", "src_app_web_run"),
    ],
)
def test_resolve(project, reference, expected):
    assert g.resolve(g.load(project), reference) == expected


def test_resolve_ambiguous_lists_candidates(project):
    with pytest.raises(g.AmbiguousNode) as error:
        g.resolve(g.load(project), "run")
    assert error.value.candidates == ["src_app_core_run", "src_app_web_run"]
    assert "src_app_core_run  run() at src/app/core.clj:" in str(error.value)


def test_resolve_prefers_defined_node_over_external(project):
    data = json.loads((project / "graphify-out/graph.json").read_text())
    data["nodes"].append({"id": "connect_external", "label": "connect()"})
    (project / "graphify-out/graph.json").write_text(json.dumps(data))
    assert g.resolve(g.load(project), "connect()") == "src_app_db_connect"


def test_resolve_missing(project):
    with pytest.raises(g.NodeNotFound):
        g.resolve(g.load(project), "nothing-here")


def test_search_ranks_label_matches_first(project):
    ids, total = g.search(g.load(project), "db", 10)
    assert total == 2 and set(ids) == {"src_app_db_query", "src_app_db_connect"}


def test_shortest_path_follows_edges_in_both_directions(project):
    steps = g.shortest_path(g.load(project), "src_app_web_run", "src_app_db_connect")
    assert [node for node, _ in steps] == [
        "src_app_web_run",
        "src_app_core_run",
        "src_app_db_query",
        "src_app_db_connect",
    ]
    assert steps[0][1] is None and steps[1][1].relation == "calls"


def test_shortest_path_unconnected_is_empty(project):
    assert g.shortest_path(g.load(project), "lonely", "src_app_core") == []


def test_affected_follows_dependents_not_containers(project):
    rows = g.affected(g.load(project), "src_app_db_connect", 5)
    assert [(node, distance) for node, distance, _ in rows] == [
        ("src_app_db_query", 1),
        ("src_app_core_run", 2),
        ("src_app_web_run", 3),
    ]


def test_affected_respects_depth(project):
    assert len(g.affected(g.load(project), "src_app_db_connect", 1)) == 1


def test_hubs_counts_relation(project):
    assert g.hubs(g.load(project), 2, frozenset({"calls"}))[0] == ("src_app_core_run", 2)


def test_search_matches_an_invalid_regex_as_text(project):
    ids, total = g.search(g.load(project), "connect(", 10)
    assert ids == ["src_app_db_connect"] and total == 1


def test_resolve_protocol_method_by_metadata_name(tmp_path):
    method = {
        "id": "src_app_store_istore_fetch",
        "label": ".-fetch()",
        "source_file": "src/app/store.clj",
        "metadata": {"language": "clojure", "kind": "var", "module": "app.store", "name": "-fetch"},
    }
    impl = {"id": "src_app_store_memstore_fetch", "label": ".-fetch()", "source_file": "src/app/store.clj"}
    graph = g.parse(tmp_path / "graph.json", {"nodes": [method, impl], "links": []})
    assert g.resolve(graph, "app.store/-fetch") == "src_app_store_istore_fetch"


def test_resolve_qualified_method_type_and_path(tmp_path):
    nodes = [
        {"id": "ledger", "label": "Ledger", "source_file": "shop/ledger.py"},
        {"id": "ledger_post", "label": ".post()", "source_file": "shop/ledger.py"},
        {"id": "client", "label": "HttpClient", "source_file": "shop/http.py"},
        {"id": "client_post", "label": ".post()", "source_file": "shop/http.py"},
        {"id": "orders_order", "label": "Order", "source_file": "orders/order.go"},
        {"id": "audit_order", "label": "Order", "source_file": "audit/audit.go"},
        {"id": "util_sum", "label": "sum()", "source_file": "src/util.rs"},
        {"id": "net_sum", "label": "sum()", "source_file": "src/net.rs"},
    ]
    links = [
        {"source": "ledger", "target": "ledger_post", "relation": "method"},
        {"source": "client", "target": "client_post", "relation": "method"},
    ]
    graph = g.parse(tmp_path / "graph.json", {"nodes": nodes, "links": links})
    assert g.resolve(graph, "Ledger.post") == "ledger_post"
    assert g.resolve(graph, "HttpClient.post()") == "client_post"
    assert g.resolve(graph, "orders.Order") == "orders_order"
    assert g.resolve(graph, "crate::util::sum") == "util_sum"


def _graph(tmp_path, name, nodes):
    return g.parse(tmp_path / name, {"nodes": [{"file_type": "code", **node} for node in nodes], "links": []})


def test_moved_ids_pair_a_renamed_definition_by_its_new_label(tmp_path):
    before = _graph(
        tmp_path,
        "before.json",
        [
            {"id": "py_util", "label": "util.py", "source_file": "py/util.py"},
            {"id": "py_util_helper", "label": "helper()", "source_file": "py/util.py"},
            {"id": "py_util_box_helper", "label": ".helper()", "source_file": "py/util.py"},
        ],
    )
    after = _graph(
        tmp_path,
        "after.json",
        [
            {"id": "py_util", "label": "util.py", "source_file": "py/util.py"},
            {"id": "py_util_assist", "label": "assist()", "source_file": "py/util.py"},
            {"id": "py_util_box_assist", "label": ".assist()", "source_file": "py/util.py"},
        ],
    )
    renamed = {"helper": "assist"}
    assert g.moved_ids(before, after, {}, renamed, {"py/util.py"}) == {
        "py_util_box_helper": "py_util_box_assist",
        "py_util_helper": "py_util_assist",
    }
    # Only a file of the change can hold a node with a new id, and an unchanged graph has none.
    assert g.moved_ids(before, after, {}, renamed, {"py/main.py"}) == {}
    assert g.moved_ids(before, before, {}, renamed, {"py/util.py"}) == {}


def test_moved_ids_keep_the_id_tail_of_a_moved_file(tmp_path):
    before = _graph(
        tmp_path,
        "before.json",
        [
            {"id": "app_util", "label": "util.py", "source_file": "app/util.py"},
            {"id": "app_util_slugify", "label": "slugify()", "source_file": "app/util.py"},
            {"id": "app_util_slug_parse", "label": ".parse()", "source_file": "app/util.py"},
            {"id": "app_util_notes", "label": "notes", "source_file": "app/util.py", "file_type": "document"},
        ],
    )
    after = _graph(
        tmp_path,
        "after.json",
        [
            {"id": "app_text", "label": "text.py", "source_file": "app/text.py"},
            {"id": "app_text_slugify", "label": "slugify()", "source_file": "app/text.py"},
            {"id": "app_text_slug_parse", "label": ".parse()", "source_file": "app/text.py"},
            {"id": "app_text_notes", "label": "notes", "source_file": "app/text.py"},
        ],
    )
    # A node of another file type is not the same node, so it pairs with nothing.
    assert g.moved_ids(before, after, {"app/util.py": "app/text.py"}, {}, {"app/util.py"}) == {
        "app_util": "app_text",
        "app_util_slug_parse": "app_text_slug_parse",
        "app_util_slugify": "app_text_slugify",
    }


def test_moved_ids_pair_only_one_to_one(tmp_path):
    before = _graph(
        tmp_path,
        "before.json",
        [
            {"id": "py_a_helper", "label": "helper()", "source_file": "py/a.py"},
            {"id": "py_a_box_helper", "label": "helper()", "source_file": "py/a.py"},
            {"id": "py_b_helper", "label": "helper()", "source_file": "py/b.py"},
        ],
    )
    after = _graph(
        tmp_path,
        "after.json",
        [
            {"id": "py_a_assist", "label": "assist()", "source_file": "py/a.py"},
            {"id": "py_b_assist", "label": "assist()", "source_file": "py/b.py"},
            {"id": "py_b_box_assist", "label": "assist()", "source_file": "py/b.py"},
        ],
    )
    # Two old nodes claim one new node in a.py, and one old node finds two new nodes in b.py.
    assert g.moved_ids(before, after, {}, {"helper": "assist"}, {"py/a.py", "py/b.py"}) == {}
