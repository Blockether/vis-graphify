import json

import pytest

NODES = [
    {
        "id": "src_app_core",
        "label": "app.core",
        "file_type": "code",
        "source_file": "src/app/core.clj",
        "source_location": "L1",
        "metadata": {"language": "clojure", "kind": "namespace", "module": "app.core"},
    },
    {
        "id": "src_app_core_run",
        "label": "run()",
        "file_type": "code",
        "source_file": "src/app/core.clj",
        "source_location": "L5",
        "metadata": {"language": "clojure", "kind": "var", "module": "app.core"},
    },
    {
        "id": "src_app_db_query",
        "label": "query()",
        "file_type": "code",
        "source_file": "src/app/db.clj",
        "source_location": "L3",
        "metadata": {"language": "clojure", "kind": "var", "module": "app.db"},
    },
    {
        "id": "src_app_db_connect",
        "label": "connect()",
        "file_type": "code",
        "source_file": "src/app/db.clj",
        "source_location": "L9",
        "metadata": {"language": "clojure", "kind": "var", "module": "app.db"},
    },
    {
        "id": "src_app_web_run",
        "label": "run()",
        "file_type": "code",
        "source_file": "src/app/web.clj",
        "source_location": "L2",
        "metadata": {"language": "clojure", "kind": "var", "module": "app.web"},
    },
    {"id": "lonely", "label": "lonely()", "file_type": "code", "source_file": "src/app/x.clj", "source_location": "L1"},
]


def _link(source, target, relation, line="L1"):
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": "EXTRACTED",
        "source_file": "src/app/core.clj",
        "source_location": line,
    }


LINKS = [
    _link("src_app_core", "src_app_core_run", "contains"),
    _link("src_app_core_run", "src_app_db_query", "calls", "L6"),
    _link("src_app_db_query", "src_app_db_connect", "calls", "L4"),
    _link("src_app_web_run", "src_app_core_run", "calls", "L3"),
]


@pytest.fixture
def project(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps({"directed": False, "nodes": NODES, "links": LINKS}))
    return tmp_path
