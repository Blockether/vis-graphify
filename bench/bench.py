"""Benchmark vis-graphify: extraction, queries and changes on real Clojure code.

Run it from the repository root:

    uv run python bench/bench.py --graphify NAME=PYTHON ... --corpus NAME=DIR ... --out bench/results.json

Each `--graphify` names a Python interpreter that has a graphify fork installed.
clj-kondo analysis is the ground truth for definitions and var references.
The benchmark makes no LLM calls and changes only copies of the corpora.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vis_graphify import graph as g  # noqa: E402
from vis_graphify import tools as t  # noqa: E402

IGNORED = shutil.ignore_patterns(
    ".git", "graphify-out", "target", ".cpcache", "node_modules", ".clj-kondo", ".lsp", ".venv", ".graphify*"
)
EDGE_RELATIONS = {"calls", "references"}
DEF_KINDS = {"var"}


def kondo(root: Path) -> tuple[set[tuple[str, str]], set[tuple[tuple[str, str], tuple[str, str]]]]:
    """Return the var definitions and the internal var-to-var usages that clj-kondo finds."""
    config = "{:output {:format :json} :analysis {:arglists false :locals false :keywords false} :linters ^:replace {}}"
    done = subprocess.run(
        ["clj-kondo", "--parallel", "--lint", str(root), "--config", config], capture_output=True, text=True
    )
    analysis = json.loads(done.stdout)["analysis"]
    defs = {(d["ns"], d["name"]) for d in analysis["var-definitions"]}
    uses = set()
    for u in analysis["var-usages"]:
        if not u.get("from-var"):
            continue
        source, target = (u["from"], u["from-var"]), (u["to"], u["name"])
        if source != target and source in defs and target in defs:
            uses.add((source, target))
    return defs, uses


def var_index(graph: g.Graph) -> dict[str, tuple[str, str]]:
    """Map each Clojure var node id to its (namespace, name)."""
    index = {}
    for node_id, node in graph.nodes.items():
        meta = node.get("metadata") or {}
        if meta.get("language") == "clojure" and meta.get("kind") in DEF_KINDS and meta.get("name"):
            index[node_id] = (meta["module"], meta["name"])
    return index


def var_edges(graph: g.Graph, index: dict) -> set:
    """The var-to-var calls and references of a graph, as ((ns, name), (ns, name)) pairs."""
    pairs = set()
    for link in graph.links:
        if link.relation in EDGE_RELATIONS and link.source in index and link.target in index:
            if index[link.source] != index[link.target]:
                pairs.add((index[link.source], index[link.target]))
    return pairs


def ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 1.0


def timed(fn, *args, **kwargs):
    started = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - started


def use_graphify(python: str) -> None:
    """Point the extension at the graphify CLI of one interpreter."""
    t.graphify_command = lambda: [python, "-m", "graphify"]


def extraction(tools: t.GraphifyTools, root: Path, truth) -> dict:
    """Cold build, no-change rebuild and accuracy against clj-kondo."""
    cold = tools.build(str(root))
    warm = tools.build(str(root))
    graph = g.load(root)
    index = var_index(graph)
    found_defs = set(index.values())
    defs, uses = truth
    edges = var_edges(graph, index)
    return {
        "cold_build_s": cold.seconds,
        "unchanged_rebuild_s": warm.seconds,
        "nodes": cold.nodes,
        "edges": cold.edges,
        "kondo_defs": len(defs),
        "def_recall": ratio(len(defs & found_defs), len(defs)),
        "def_precision": ratio(len(found_defs & defs), len(found_defs)),
        "kondo_uses": len(uses),
        "use_recall": ratio(len(uses & edges), len(uses)),
        "use_precision": ratio(len(edges & uses), len(edges)),
    }


def node_for(graph: g.Graph, index: dict, var: tuple[str, str]) -> str | None:
    return next((nid for nid, v in index.items() if v == var), None)


def queries(tools: t.GraphifyTools, root: Path, truth, samples: int, seed: int) -> dict:
    """Latency and correctness of the query verbs, on sampled vars."""
    graph = g.load(root)
    index = var_index(graph)
    defs, uses = truth
    callers: dict = {}
    for source, target in uses:
        callers.setdefault(target, set()).add(source)
    rng = random.Random(seed)
    targets = rng.sample(sorted(callers), min(samples, len(callers)))
    lat = {"resolve_qualified": [], "search": [], "explain": [], "affected": [], "shortest_path": []}
    resolved = explain_hits = explain_total = affected_hits = path_found = 0
    unresolved = []
    for ns, name in targets:
        ref = f"{ns}/{name}"
        try:
            _, s = timed(tools.explain, ref, str(root), 500)
            resolved += 1
        except (LookupError, ValueError) as error:
            unresolved.append(f"{ref}: {type(error).__name__}")
            continue
        lat["resolve_qualified"].append(s)
        explanation, s = timed(tools.explain, ref, str(root), 500)
        lat["explain"].append(s)
        got = {e.node.id for e in explanation.incoming}
        expected = callers[(ns, name)]
        explain_total += len(expected)
        explain_hits += sum(1 for c in expected if node_for(graph, index, c) in got)
        impact, s = timed(tools.affected, ref, str(root), 1, 1000)
        lat["affected"].append(s)
        impacted = {i.node.id for i in impact.impacts}
        affected_hits += sum(1 for c in expected if node_for(graph, index, c) in impacted)
        _, s = timed(tools.search, name, str(root), 25)
        lat["search"].append(s)
        caller = sorted(expected)[0]
        try:
            path, s = timed(tools.shortest_path, f"{caller[0]}/{caller[1]}", ref, str(root))
            lat["shortest_path"].append(s)
            path_found += 1 if path.found else 0
        except (LookupError, ValueError):
            pass
    _, hubs_s = timed(tools.hubs, str(root), 20)
    answer, query_s = timed(tools.query, "How are requests routed between components?", str(root))
    return {
        "sampled_vars": len(targets),
        "qualified_name_resolved": ratio(resolved, len(targets)),
        "unresolved_examples": unresolved[:5],
        "explain_caller_recall": ratio(explain_hits, explain_total),
        "affected_depth1_caller_recall": ratio(affected_hits, explain_total),
        "shortest_path_found": ratio(path_found, resolved),
        "hubs_ms": round(hubs_s * 1000, 1),
        "query_s": round(query_s, 2),
        "query_answer_chars": len(answer.text),
        **{f"{k}_ms_p50": round(statistics.median(v) * 1000, 1) for k, v in lat.items() if v},
    }


def first_def(path: Path) -> tuple[str, str] | None:
    """The namespace and the first public defn name of a Clojure file."""
    text = path.read_text()
    ns = re.search(r"^\(ns\s+([\w.\-]+)", text, re.M)
    fn = re.search(r"^\(defn\s+([\w\-!?*<>=]+)", text, re.M)
    return (ns.group(1), fn.group(1)) if ns and fn else None


def snapshot(graph: g.Graph) -> tuple[set, set]:
    nodes = {n for n in graph.nodes if graph.nodes[n].get("file_type") == "code"}
    edges = {(link.source, link.relation, link.target) for link in graph.links if link.source in nodes}
    return nodes, edges


def changes(tools: t.GraphifyTools, root: Path) -> dict:
    """Edit a copy, rebuild incrementally, and check the graph against the edit and a full build."""
    files = sorted(p for p in root.rglob("*.clj") if first_def(p))
    a, b = files[0], files[-1]
    a_ns, a_fn = first_def(a)
    b_ns, b_fn = first_def(b)
    checks = {}
    timings = {}

    # 1. Add a var in namespace A that calls a var of A.
    with a.open("a") as out:
        out.write(f"\n(defn bench-probe-caller [x]\n  ({a_fn} x))\n")
    res = tools.build(str(root))
    timings["add_var_s"] = res.seconds
    impact = tools.affected(f"{a_ns}/{a_fn}", str(root), 1, 1000)
    checks["added_var_in_graph"] = tools.search("^bench-probe-caller", str(root)).total > 0
    checks["added_call_in_affected"] = any(
        i.node.label.removesuffix("()") == "bench-probe-caller" for i in impact.impacts
    )

    # 2. Rename var B and every use of it in its own file.
    text = b.read_text()
    b.write_text(text.replace(b_fn, b_fn + "-renamed"))
    res = tools.build(str(root))
    timings["rename_var_s"] = res.seconds
    found_old = tools.search(f"^{re.escape(b_fn)}(\\(\\))?$", str(root)).nodes
    found_new = tools.search(f"^{re.escape(b_fn)}-renamed(\\(\\))?$", str(root)).nodes
    checks["renamed_old_gone"] = not any(
        n.label.removesuffix("()") == b_fn and n.file.endswith(b.name) for n in found_old
    )
    checks["renamed_new_present"] = bool(found_new)

    # 3. Delete one file.
    gone = files[len(files) // 2]
    rel = str(gone.relative_to(root))
    gone.unlink()
    res = tools.build(str(root), force=True)
    timings["delete_file_s"] = res.seconds
    graph = g.load(root)
    checks["deleted_file_nodes_gone"] = not any(n.get("source_file") == rel for n in graph.nodes.values())

    # 4. The incremental graph equals a full build of the same tree.
    incremental = snapshot(graph)
    fresh = Path(tempfile.mkdtemp(prefix="gfy-fresh-")) / root.name
    shutil.copytree(root, fresh, ignore=IGNORED)
    full = tools.build(str(fresh))
    timings["full_build_s"] = full.seconds
    rebuilt = snapshot(g.load(fresh))
    nodes_j = ratio(len(incremental[0] & rebuilt[0]), len(incremental[0] | rebuilt[0]))
    edges_j = ratio(len(incremental[1] & rebuilt[1]), len(incremental[1] | rebuilt[1]))
    shutil.rmtree(fresh.parent)
    return {
        **checks,
        **timings,
        "incremental_vs_full_node_jaccard": nodes_j,
        "incremental_vs_full_edge_jaccard": edges_j,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--graphify", action="append", required=True, help="NAME=PYTHON")
    parser.add_argument("--corpus", action="append", required=True, help="NAME=DIR")
    parser.add_argument("--samples", type=int, default=150)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="gfy-bench-"))
    results = []
    for corpus in args.corpus:
        cname, cdir = corpus.split("=", 1)
        truth = kondo(Path(cdir).expanduser())
        for spec in args.graphify:
            gname, python = spec.split("=", 1)
            use_graphify(python)
            tools = t.GraphifyTools()
            root = work / gname / cname
            shutil.copytree(Path(cdir).expanduser(), root, ignore=IGNORED)
            row = {"graphify": gname, "corpus": cname}
            row["extraction"] = extraction(tools, root, truth)
            row["queries"] = queries(tools, root, truth, args.samples, args.seed)
            row["changes"] = changes(tools, root)
            print(json.dumps(row), flush=True)
            results.append(row)
    if args.out:
        args.out.write_text(json.dumps(results, indent=2) + "\n")
    shutil.rmtree(work)


if __name__ == "__main__":
    main()
