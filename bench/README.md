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
- **Queries**: for 150 sampled vars that have callers, whether `explain("ns/name")` resolves, whether `explain()` and `affected(depth=1)` list every clj-kondo caller, and whether `shortest_path()` connects a caller to the var. It also times `search`, `hubs` and `query`.
- **Changes**: add a var that calls an existing var, rename a var in its file, and delete a file. After each incremental build, it checks that the graph and `affected()` show the change. Then it compares the incremental graph with a full build of the same tree.

## Results

Apple M-series laptop, Python 3.13, 2026-10-21. Corpora: svar (11 files, 0.9 MiB), spel (48 files, 2.0 MiB) and Vis `src` (235 files, 6.4 MiB). Both rows use extension 0.2.3. `v0.9.84-blockether.5` is the fork before the rebase on upstream v0.9.93.

### Extraction

| graphify | corpus | cold build s | no-change rebuild s | def recall | ref recall | ref precision |
|---|---|---|---|---|---|---|
| v0.9.84-blockether.5 | svar | 0.92 | 0.44 | 0.9975 | 0.9887 | 0.9992 |
| v0.9.93-blockether.2 | svar | 1.08 | 0.38 | 0.9975 | 0.9887 | 0.9992 |
| v0.9.84-blockether.5 | spel | 2.06 | 1.01 | 0.9947 | 0.9853 | 0.9801 |
| v0.9.93-blockether.2 | spel | 1.84 | 0.79 | 0.9947 | 0.9853 | 0.9801 |
| v0.9.84-blockether.5 | vis | 6.11 | 3.35 | 0.9986 | 0.9964 | 0.9993 |
| v0.9.93-blockether.2 | vis | 5.19 | 2.37 | 0.9986 | 0.9964 | 0.9993 |

### Queries

| graphify | corpus | `ns/name` resolved | explain caller recall | affected caller recall | path found | explain ms p50 | search ms p50 | path ms p50 | query s |
|---|---|---|---|---|---|---|---|---|---|
| v0.9.84-blockether.5 | svar | 1.0 | 0.9898 | 0.9898 | 1.0 | 0.4 | 0.9 | 0.7 | 0.17 |
| v0.9.93-blockether.2 | svar | 1.0 | 0.9898 | 0.9898 | 1.0 | 0.4 | 0.9 | 0.7 | 0.16 |
| v0.9.84-blockether.5 | spel | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 2.7 | 2.1 | 0.25 |
| v0.9.93-blockether.2 | spel | 1.0 | 1.0 | 1.0 | 1.0 | 1.1 | 2.7 | 2.1 | 0.25 |
| v0.9.84-blockether.5 | vis | 1.0 | 0.9975 | 0.9975 | 1.0 | 3.2 | 9.6 | 6.4 | 0.69 |
| v0.9.93-blockether.2 | vis | 1.0 | 0.9975 | 0.9975 | 1.0 | 3.3 | 9.6 | 6.6 | 0.64 |

### Changes

| graphify | corpus | add var s | rename var s | delete file s | full build s | checks passed | incremental = full (nodes, edges) |
|---|---|---|---|---|---|---|---|
| v0.9.84-blockether.5 | svar | 0.82 | 0.78 | 0.78 | 0.8 | 5/5 | 1.0, 1.0 |
| v0.9.93-blockether.2 | svar | 0.71 | 0.71 | 0.72 | 0.75 | 5/5 | 1.0, 1.0 |
| v0.9.84-blockether.5 | spel | 2.09 | 2.14 | 2.11 | 2.06 | 5/5 | 1.0, 1.0 |
| v0.9.93-blockether.2 | spel | 1.89 | 1.9 | 1.94 | 1.82 | 5/5 | 1.0, 1.0 |
| v0.9.84-blockether.5 | vis | 7.0 | 7.09 | 7.09 | 6.02 | 5/5 | 1.0, 1.0 |
| v0.9.93-blockether.2 | vis | 6.15 | 6.07 | 6.1 | 5.12 | 5/5 | 1.0, 1.0 |

## Findings

- The rebase on upstream v0.9.93 keeps the Clojure extraction exact: the same nodes, edges, recall and precision on all three corpora.
- Clojure definitions have a recall of 0.995 to 0.999 and a precision of 1.0. Var-to-var references have a recall of 0.985 to 0.996 and a precision of 0.980 to 0.999.
- `explain()` and `affected()` find 99 to 100 percent of the callers. A query takes milliseconds on a graph of 15,000 nodes.
- Each change is in the graph after one incremental build, and the incremental graph is equal to a full build.
- An incremental build is not faster than a full build. Community clustering runs on the full graph after each change: about 4 s of the 6 s on Vis. `build(cluster=False)` updates Vis in about 2 s.
- Extension 0.2.3 fixes two query problems that this benchmark found. `explain("ns/name")` did not resolve Clojure protocol methods (1 to 2 percent of the sampled vars). `search()` failed on names that are not valid regular expressions, such as `*out*`.
- graphify v0.9.93-blockether.2 reads the Clojure forms for domain plugins only when a plugin applies. This makes a one-file update of Vis without clustering 36 percent faster (3.0 s to 1.9 s).

The raw numbers are in [`results.json`](results.json).
