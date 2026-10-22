# vis-graphify benchmark

This benchmark measures what an agent gets from the extension: how well it extracts code facts, how fast and how correctly it answers queries, and how well the graph follows code changes. It makes no LLM calls.

## Run it

```bash
uv run python bench/bench.py \
  --graphify old=/path/to/old/venv/bin/python \
  --graphify new=.venv/bin/python \
  --corpus svar=~/svar/src --corpus spel=~/spel/src --corpus vis=~/vis/src \
  --samples 150 --out bench/results.json
```

- Each `--graphify` names an interpreter with one graphify fork installed. The benchmark runs the extension tools (`GraphifyTools`) with that graphify.
- Each corpus is copied to a temporary directory. The benchmark changes only the copies.
- [clj-kondo](https://github.com/clj-kondo/clj-kondo) analysis is the ground truth: its var definitions, and its var usages between two vars of the corpus.
- Builds use the extension defaults, with community clustering.

## What it measures

- **Extraction**: the cold build, a rebuild without changes, and the recall and precision of definitions and var-to-var references (`calls` and `references` edges).
- **Queries**: for 150 sampled vars that have callers, whether `explain("ns/name")` resolves, whether `explain()` and `affected(depth=1)` list every clj-kondo caller, and whether `shortest_path()` connects a caller to the var. It also times `search` and `hubs`.
- **Changes**: add a var that calls an existing var, rename a var in its file, and delete a file. After each incremental build, it checks that the graph and `affected()` show the change. Then it compares the incremental graph with a full build of the same tree.

## Results

Apple M-series laptop, Python 3.13, 2026-10-22. Corpora: svar (11 files, 0.9 MiB), spel (48 files, 2.0 MiB) and Vis `src` (235 files, 6.4 MiB). Every row runs the current extension code. A row name gives the release whose graphify fork and clustering it uses:

- `0.2.2`: graphify `v0.9.84-blockether.5`, before the rebase on upstream v0.9.93.
- `0.2.3`: graphify `v0.9.93-blockether.2`, networkx Louvain clustering.
- `0.2.4`: graphify `v0.9.93-blockether.3` with native Leiden clustering (`graspologic-native`).

The first cold build of a new environment also compiles Python bytecode. This explains the slow svar cold build of `0.2.3`.

### Extraction

| graphify | corpus | cold build s | no-change rebuild s | def recall | ref recall | ref precision |
|---|---|---|---|---|---|---|
| 0.2.2 (v0.9.84-blockether.5) | svar | 0.94 | 0.46 | 0.9975 | 0.9887 | 0.9992 |
| 0.2.3 (v0.9.93-blockether.2) | svar | 1.86 | 0.37 | 0.9975 | 0.9887 | 0.9992 |
| 0.2.4 (v0.9.93-blockether.3 + Leiden) | svar | 0.71 | 0.4 | 0.9975 | 0.9887 | 0.9992 |
| 0.2.2 (v0.9.84-blockether.5) | spel | 2.07 | 1.04 | 0.9947 | 0.9853 | 0.9801 |
| 0.2.3 (v0.9.93-blockether.2) | spel | 1.82 | 0.78 | 0.9947 | 0.9853 | 0.9801 |
| 0.2.4 (v0.9.93-blockether.3 + Leiden) | spel | 1.55 | 0.83 | 0.9947 | 0.9853 | 0.9801 |
| 0.2.2 (v0.9.84-blockether.5) | vis | 6.37 | 3.43 | 0.9986 | 0.9964 | 0.9993 |
| 0.2.3 (v0.9.93-blockether.2) | vis | 5.3 | 2.44 | 0.9986 | 0.9964 | 0.9993 |
| 0.2.4 (v0.9.93-blockether.3 + Leiden) | vis | 4.36 | 2.57 | 0.9986 | 0.9964 | 0.9993 |

### Queries

| graphify | corpus | `ns/name` resolved | explain caller recall | affected caller recall | path found | explain ms p50 | search ms p50 | path ms p50 | query s |
|---|---|---|---|---|---|---|---|---|---|
| 0.2.2 (v0.9.84-blockether.5) | svar | 1.0 | 0.9898 | 0.9898 | 1.0 | 0.4 | 1.0 | 0.7 | 0.16 |
| 0.2.3 (v0.9.93-blockether.2) | svar | 1.0 | 0.9898 | 0.9898 | 1.0 | 0.4 | 0.9 | 0.7 | 0.18 |
| 0.2.4 (v0.9.93-blockether.3 + Leiden) | svar | 1.0 | 0.9898 | 0.9898 | 1.0 | 0.4 | 0.9 | 0.7 | 0.17 |
| 0.2.2 (v0.9.84-blockether.5) | spel | 1.0 | 1.0 | 1.0 | 1.0 | 1.1 | 2.7 | 2.1 | 0.29 |
| 0.2.3 (v0.9.93-blockether.2) | spel | 1.0 | 1.0 | 1.0 | 1.0 | 1.1 | 2.8 | 2.2 | 0.26 |
| 0.2.4 (v0.9.93-blockether.3 + Leiden) | spel | 1.0 | 1.0 | 1.0 | 1.0 | 1.1 | 2.7 | 2.1 | 0.26 |
| 0.2.2 (v0.9.84-blockether.5) | vis | 1.0 | 0.9975 | 0.9975 | 1.0 | 3.3 | 9.9 | 6.5 | 0.66 |
| 0.2.3 (v0.9.93-blockether.2) | vis | 1.0 | 0.9975 | 0.9975 | 1.0 | 3.4 | 10.0 | 7.1 | 0.66 |
| 0.2.4 (v0.9.93-blockether.3 + Leiden) | vis | 1.0 | 0.9975 | 0.9975 | 1.0 | 3.8 | 10.3 | 8.0 | 0.67 |

### Changes

| graphify | corpus | add var s | rename var s | delete file s | full build s | checks passed | incremental = full (nodes, edges) |
|---|---|---|---|---|---|---|---|
| 0.2.2 (v0.9.84-blockether.5) | svar | 0.81 | 0.82 | 0.81 | 0.83 | 5/5 | 1.0, 1.0 |
| 0.2.3 (v0.9.93-blockether.2) | svar | 0.72 | 0.71 | 0.71 | 0.75 | 5/5 | 1.0, 1.0 |
| 0.2.4 (v0.9.93-blockether.3 + Leiden) | svar | 0.66 | 0.65 | 0.64 | 0.67 | 5/5 | 1.0, 1.0 |
| 0.2.2 (v0.9.84-blockether.5) | spel | 2.14 | 2.22 | 2.21 | 2.04 | 5/5 | 1.0, 1.0 |
| 0.2.3 (v0.9.93-blockether.2) | spel | 1.88 | 1.92 | 1.9 | 1.78 | 5/5 | 1.0, 1.0 |
| 0.2.4 (v0.9.93-blockether.3 + Leiden) | spel | 1.59 | 1.63 | 1.58 | 1.54 | 5/5 | 1.0, 1.0 |
| 0.2.2 (v0.9.84-blockether.5) | vis | 7.3 | 7.18 | 7.19 | 6.18 | 5/5 | 1.0, 1.0 |
| 0.2.3 (v0.9.93-blockether.2) | vis | 6.2 | 6.22 | 6.27 | 5.45 | 5/5 | 1.0, 1.0 |
| 0.2.4 (v0.9.93-blockether.3 + Leiden) | vis | 5.19 | 5.14 | 5.1 | 4.07 | 5/5 | 1.0, 1.0 |

## Findings

- All three releases extract the same Clojure facts: the same nodes, edges, recall and precision on all three corpora.
- Clojure definitions have a recall of 0.995 to 0.999 and a precision of 1.0. Var-to-var references have a recall of 0.985 to 0.996 and a precision of 0.980 to 0.999.
- `explain()` and `affected()` find 99 to 100 percent of the callers. A query takes milliseconds on a graph of 15,000 nodes.
- Each change is in the graph after one incremental build, and the incremental graph is equal to a full build.
- On Vis, a one-file update takes 5.2 s instead of 7.3 s in `0.2.2`, and a full build 4.1 s instead of 6.2 s. Three changes give this:
  - graphify reads the Clojure forms for domain plugins only when a plugin applies (`v0.9.93-blockether.2`).
  - The betweenness centrality for the suggested questions of `GRAPH_REPORT.md` runs over indexed lists. It gives bit-identical scores in 1.0 s instead of 2.6 s (`v0.9.93-blockether.3`).
  - Native Leiden clusters in 0.5 s instead of 1.05 s for networkx Louvain. The communities differ from Louvain communities, and they stay deterministic.
- An incremental build is still not faster than a full build. Clustering, the report and the graph comparison run on the full graph after each change. `build(cluster=False)` updates Vis in about 2 s, but it drops the communities.
- Extension 0.2.3 fixed two query problems that this benchmark found: `explain("ns/name")` of Clojure protocol methods, and `search()` of names that are not valid regular expressions, such as `*out*`.

The raw numbers are in [`results.json`](results.json).
