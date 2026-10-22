# Harbor evals: Vis with and without vis-graphify

## When to use

- You change a graphify tool and want to know if agents still finish structural tasks.
- You want to compare token cost and wall time with and without the extension.
- For tool speed on real repositories without a model, use `bench/` instead.

## Tasks

`make_tasks.py` writes six Harbor tasks to `evals/tasks/`. Each task is a small multi-file project in one language. Each project has a decoy: a second symbol with the same name that must stay unchanged. 80 filler modules also name the symbol in comments and strings. A plain text replace breaks the project.

| Task | Language | Change |
|---|---|---|
| `py-rename-method` | Python | Rename `Ledger.post`, keep `HttpClient.post`. |
| `py-impact` | Python | List the handlers that can reach `app.db.execute_raw` in `impact.json`. |
| `ts-rename-export` | TypeScript | Rename an exported function through aliases and re-exports. |
| `go-rename-type` | Go | Rename `orders.Order`, keep `audit.Order`. |
| `rs-rename-fn` | Rust | Rename `util::checksum`, keep `Packet::checksum`. |
| `clj-rename-var` | Clojure | Rename `shop.pricing/discount`, keep `shop.coupons/discount`. |

The verifier grades the trial with `blockether.vis.evals.grade`. Then it runs the project check: `tsc`, `go vet` and `go run`, `cargo run`, `clojure -M -m shop.main` or a Python check. A trial passes only when both pass. A raised tool error also fails the trial.

## Run

You need Harbor, Vis and the toolchains of the six languages (`node`, `go`, `cargo`, `clojure`).

1. Write the tasks: `python evals/make_tasks.py`.
2. Run the vanilla arm:

   ```bash
   harbor run --path evals/tasks --agent blockether.vis.evals.agent:VisAgent \
     --env blockether.vis.evals.environment:HostEnvironment \
     --model anthropic-coding-plan/claude-opus-5-5 --n-attempts 3 -n 4 --job-name vanilla
   ```

3. Copy the installed extension into an empty directory, for example `ext/vis-graphify/<version>` with `current` pointing to it. Use a real copy, not a symlink: Vis ignores an extension whose path is outside the directory.
4. Run the graphify arm with the same command, plus `--ae VIS_EXTENSIONS_DIR=<that directory>` and `--job-name graphify`.

Each trial writes `verifier/grade.json` (tokens, wall time, tools, output size) and `verifier/reward.json`.

## Results

Model `claude-opus-5-5`. Vanilla: 7 trials for each task from three rounds. Graphify 0.3.1: 2 trials. Graphify 0.3.3: 3 trials. Tokens and output are means for each trial.

| Task | Vanilla pass | Wall | Tokens | Output | 0.3.1 pass | Tokens | Output | 0.3.3 pass | Wall | Tokens | Output |
|---|---|---|---|---|---|---|---|---|---|---|---|
| clj-rename-var | 6/7 | 46 s | 107k | 11.5k | 2/2 | 117k | 43.9k | 3/3 | 31 s | 88k | 13.0k |
| go-rename-type | 7/7 | 39 s | 97k | 10.0k | 1/2 | 125k | 12.7k | 3/3 | 35 s | 93k | 14.5k |
| py-impact | 7/7 | 38 s | 83k | 9.2k | 2/2 | 58k | 8.8k | 3/3 | 40 s | 92k | 10.3k |
| py-rename-method | 7/7 | 40 s | 104k | 17.4k | 2/2 | 125k | 27.0k | 3/3 | 37 s | 105k | 15.3k |
| rs-rename-fn | 7/7 | 51 s | 133k | 17.0k | 2/2 | 175k | 145.9k | 3/3 | 25 s | 51k | 9.3k |
| ts-rename-export | 7/7 | 30 s | 84k | 13.6k | 2/2 | 104k | 12.7k | 3/3 | 38 s | 109k | 17.8k |

With 0.3.3, all 18 trials pass. The sum of the mean tokens is 12% lower than vanilla (538k against 608k), and the sum of the mean wall times is 16% lower (206 s against 244 s). The largest gain is the Rust rename: 51k tokens against 133k. In `py-impact`, the agents used `grep` only, so that row shows run-to-run noise.

## What the evals changed

- 0.3.1 printed every text mention of a rename and the full build log. One Rust trial printed 160k characters because `grep` matched `graphify-out/graph.html`. 0.3.2 prints a short summary, keeps only code mentions in `mentions`, counts the rest in `text_mentions` and writes `graphify-out/.gitignore`.
- Agents named symbols as `Order` (ambiguous) or `src/util.rs::checksum`. 0.3.2 lists each candidate with its location, and 0.3.3 accepts `file::name`.
- Agents iterated over `graphify.search(...)`. 0.3.2 makes search, references and affected results iterable.
