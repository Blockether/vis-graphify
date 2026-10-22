import json

import pytest

from vis_graphify import graph as g


def test_load_reuses_unchanged_graph_and_reloads_changed(project):
    first = g.load(project)
    assert g.load(project) is first
    data = json.loads((project / "graphify-out/graph.json").read_text())
    data["nodes"].append({"id": "new", "label": "new()"})
    (project / "graphify-out/graph.json").write_text(json.dumps(data))
    assert "new" in g.load(project).nodes


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
