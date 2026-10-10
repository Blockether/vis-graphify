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
