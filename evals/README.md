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
| `ts-impact` | TypeScript | List the handlers that can reach `executeRaw` in `src/db.ts`, through `import * as` modules. |
| `clj-impact` | Clojure | List the handlers that can reach `shop.db/raw-query`. |
| `go-add-param` | Go | Add `currency string` to `pricing.Total` and pass `"EUR"`, keep `audit.Total` and `Cart.Total`. |
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

Model `claude-opus-5-5`. Vanilla and graphify 0.5.3: 3 trials for each task. Tokens and model calls are means for each trial. Each model call sends the system prompt again, so the call count sets most of the token cost.

| Task | Vanilla pass | Tokens | Calls | Graphify pass | Tokens | Calls |
|---|---|---|---|---|---|---|
| clj-impact | 2/3 | 127k | 8 | 3/3 | 36k | 3 |
| clj-rename-var | 2/3 | 102k | 6.3 | 3/3 | 23k | 2 |
| go-add-param | 3/3 | 83k | 5.3 | 3/3 | 23k | 2 |
| go-rename-type | 3/3 | 80k | 5.3 | 3/3 | 23k | 2 |
| py-impact | 3/3 | 88k | 6 | 3/3 | 36k | 3 |
| py-rename-method | 3/3 | 125k | 7 | 3/3 | 23k | 2 |
| rs-rename-fn | 3/3 | 120k | 6.7 | 3/3 | 23k | 2 |
| ts-impact | 3/3 | 115k | 6.7 | 3/3 | 36k | 3 |
| ts-rename-export | 3/3 | 79k | 5 | 3/3 | 24k | 2 |

Graphify passes 27 of 27 trials and vanilla passes 25 of 27. The mean for each trial is 27k tokens against 102k: 73% fewer. The sum of the mean tokens over the nine tasks is 247k against 919k. A rename or `add_parameter` task takes 2 model calls: one graphify call, then the answer. An impact task takes 3: `affected`, the write of `impact.json`, then the answer. Wall times are not compared, because the first model response sometimes took 1 to 4 minutes.

### Earlier results (0.4)

Model `claude-opus-5-5`. Vanilla: 3 trials for each task. Graphify: 6 trials, 3 with 0.4.0 and 3 with 0.4.1. Tokens, calls and wall time are means for each trial.

| Task | Vanilla pass | Wall | Tokens | Calls | Graphify pass | Wall | Tokens | Calls |
|---|---|---|---|---|---|---|---|---|
| clj-impact | 2/3 | 59 s | 127k | 8.0 | 6/6 | 37 s | 92k | 5.3 |
| clj-rename-var | 2/3 | 37 s | 102k | 6.3 | 6/6 | 41 s | 115k | 7.0 |
| go-add-param | 3/3 | 32 s | 83k | 5.3 | 6/6 | 33 s | 93k | 6.2 |
| go-rename-type | 3/3 | 32 s | 80k | 5.3 | 6/6 | 34 s | 104k | 6.5 |
| py-impact | 3/3 | 38 s | 88k | 6.0 | 6/6 | 30 s | 68k | 4.2 |
| py-rename-method | 3/3 | 33 s | 125k | 7.0 | 5/6 | 45 s | 122k | 7.5 |
| rs-rename-fn | 3/3 | 44 s | 120k | 6.7 | 6/6 | 39 s | 104k | 6.3 |
| ts-impact | 3/3 | 37 s | 115k | 6.7 | 6/6 | 34 s | 81k | 4.7 |
| ts-rename-export | 3/3 | 28 s | 79k | 5.0 | 6/6 | 35 s | 97k | 5.8 |

Graphify passes 53 of 54 trials and vanilla passes 25 of 27. The one graphify failure is a `grep` error of the agent on a missing folder; the grader fails a trial on any tool error. In the three impact tasks, graphify uses 27% fewer tokens (80k against 110k) and passes 18 of 18 against 8 of 9. In the small rename tasks, both arms need about the same tokens: the agents still read files and run the project checks before and after the change. The sum of the mean tokens over the nine tasks is 876k for graphify and 920k for vanilla.

### Earlier results (0.3)

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
- Agents called `graphify.build()` first, or got a stale graph after an edit. 0.4.0 builds a missing graph and updates a stale one in each query.
- In `py-impact`, `affected` stopped after two steps. 0.4.0 follows every step by default, and `within="app/handlers/"` keeps one folder.
- In `ts-impact`, calls through `import * as repo` had no edge. graphify v0.9.93-blockether.8 resolves them.
- `go-add-param` asks for a change beyond a rename. 0.4.0 adds `graphify.add_parameter`, which changes the definition and every exact call in one call.
- After a rename, agents read the files again because the diff was cut at 60 lines. 0.4.1 prints each changed line instead.
- After a rename, agents still listed the project, read files and ran `go vet` or `tsc` themselves. 0.5.0 runs the project check after each write and prints `Check ...: passed`. The prompt tells agents to make the graphify call first and to answer from its result.
- In the impact tasks, agents read the handler files to confirm indirect calls and grepped for other definitions. 0.5.1 prints the nodes between each dependent and the change, and 0.5.2 lists the other symbols that spell the name.
- 0.5.2 listed only the places of other symbols with the same name, and agents opened each file to see what they were. 0.5.3 names each other node and the files that use it. It also ends with the complete list of dependent functions and marks the private ones.
