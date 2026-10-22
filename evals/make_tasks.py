"""Write the Harbor tasks that compare Vis with and without vis-graphify.

Each task is a structural change or question in a small multi-file project. Each
project has a decoy: a second symbol with the same name that must stay. A text
search finds the decoy too, so the agent must know which symbol each site uses.

Run `python evals/make_tasks.py` again after you change this file. The script
replaces `evals/tasks/`.
"""

from __future__ import annotations

import json
import random
import shutil
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE / "tasks"

TEST_SH = """#!/usr/bin/env bash
# Grade the trial, then run the project check. The Vis host environment sets the VIS_HARBOR_* paths.
set -uo pipefail
"$VIS_HARBOR_PYTHON" -m blockether.vis.evals.grade "$VIS_HARBOR_TESTS/checks.json" || exit 1
cd "$VIS_HARBOR_WORKSPACE" || exit 1
if ({check}) >"$VIS_HARBOR_VERIFIER_LOGS/project-check.log" 2>&1; then ok=1; else ok=0; fi
exec "$VIS_HARBOR_PYTHON" - "$VIS_HARBOR_VERIFIER_LOGS/reward.json" "$ok" <<'PY'
import json
import sys

path, ok = sys.argv[1], float(sys.argv[2])
with open(path) as handle:
    rewards = json.load(handle)
rewards["project_check"] = ok
rewards["reward"] = min(rewards["reward"], ok)
with open(path, "w") as handle:
    handle.write(json.dumps(rewards, indent=2) + "\\n")
PY
"""

PLAIN_TEST_SH = """#!/usr/bin/env bash
# Grade the trial. The Vis host environment sets the VIS_HARBOR_* paths.
set -euo pipefail
exec "$VIS_HARBOR_PYTHON" -m blockether.vis.evals.grade "$VIS_HARBOR_TESTS/checks.json"
"""


def dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def write_task(name, lang, instruction, files, checks, check=None, check_py=None, words=()):
    """Write one task folder: seeds with filler modules that name `words`, instruction, checks and verifier."""
    if words:
        files = {**noise(lang, list(words)), **files}
    root = TASKS / name
    workspace = root / "environment" / "workspace"
    for rel, text in files.items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
    if check_py:
        (tests / "check.py").write_text(check_py)
    test_sh = TEST_SH.format(check=check) if check else PLAIN_TEST_SH
    (tests / "test.sh").write_text(test_sh)
    (tests / "test.sh").chmod(0o755)
    (root / "instruction.md").write_text(dedent(instruction))
    (root / "task.toml").write_text(
        f'[metadata]\nlang = "{lang}"\n\n[agent]\ntimeout_sec = 900.0\n\n[verifier]\ntimeout_sec = 300.0\n'
    )


NOISE_FILES = 80


def noise(lang: str, words: list[str], count: int = NOISE_FILES) -> dict[str, str]:
    """Filler modules of a medium codebase. They name the task words only in comments and strings."""
    rng = random.Random(lang)
    out: dict[str, str] = {}
    for i in range(count):
        word = words[i % len(words)]
        verbs = rng.sample(["load", "sum", "merge", "split", "render", "parse", "check", "index"], 3)
        if lang == "python":
            body = [f'"""Report helpers {i}. The {word} flow lives in another module."""', ""]
            for verb in verbs:
                body += [
                    "",
                    f"def {verb}_report_{i}(rows):",
                    f"    # Do not call {word} here: this module only formats rows.",
                    "    total = sum(len(str(row)) for row in rows)",
                    f'    return {{"label": "{word} {verb}", "total": total}}',
                ]
            out[f"misc/report_{i:02d}.py"] = "\n".join(body) + "\n"
        elif lang == "typescript":
            body = [f"// Report helpers {i}. The {word} flow lives in another module.", ""]
            for verb in verbs:
                body += [
                    f"export function {verb}Report{i}(rows: string[]): {{ label: string; total: number }} {{",
                    f"  // Do not call {word} here: this module only formats rows.",
                    f'  return {{ label: "{word} {verb}", total: rows.join("").length }};',
                    "}",
                    "",
                ]
            out[f"src/misc/report{i:02d}.ts"] = "\n".join(body)
        elif lang == "go":
            body = ["package misc", "", f"// Report helpers {i}. The {word} flow lives in another package.", ""]
            for verb in verbs:
                name = verb.capitalize()
                body += [
                    f"// {name}Report{i} formats rows. Do not call {word} here.",
                    f"func {name}Report{i}(rows []string) (string, int) {{",
                    "\ttotal := 0",
                    "\tfor _, row := range rows {",
                    "\t\ttotal += len(row)",
                    "\t}",
                    f'\treturn "{word} {verb}", total',
                    "}",
                    "",
                ]
            out[f"misc/report{i:02d}.go"] = "\n".join(body)
        elif lang == "rust":
            body = [f"//! Report helpers {i}. The {word} flow lives in another module.", ""]
            for verb in verbs:
                body += [
                    f"/// Formats rows. Do not call {word} here.",
                    f"pub fn {verb}_report_{i}(rows: &[&str]) -> (&'static str, usize) {{",
                    f'    ("{word} {verb}", rows.iter().map(|row| row.len()).sum())',
                    "}",
                    "",
                ]
            out[f"src/misc/report_{i:02d}.rs"] = "\n".join(body)
        elif lang == "clojure":
            body = [
                f"(ns shop.misc.report-{i}",
                f'  "Report helpers {i}. The {word} flow lives in another namespace.")',
                "",
            ]
            for verb in verbs:
                body += [
                    f"(defn {verb}-report",
                    f'  "Format rows. Do not call {word} here."',
                    "  [rows]",
                    f'  {{:label "{word} {verb}" :total (reduce + (map count rows))}})',
                    "",
                ]
            out[f"src/shop/misc/report_{i}.clj"] = "\n".join(body)
    if lang == "rust":
        out["src/misc/mod.rs"] = "#![allow(dead_code)]\n" + "".join(f"pub mod report_{i:02d};\n" for i in range(count))
    return out


# Python: rename a method that shares its name with a method of another class.


def py_rename_method():
    files = {
        "shop/__init__.py": "",
        "shop/billing/__init__.py": "",
        "shop/ledger.py": dedent("""
            class Ledger:
                def __init__(self):
                    self.entries = []

                def post(self, account, amount):
                    self.entries.append((account, amount))
                    return len(self.entries)
            """),
        "shop/http.py": dedent("""
            class HttpClient:
                def __init__(self):
                    self.sent = []

                def post(self, url, body):
                    self.sent.append((url, body))
                    return 200
            """),
        "shop/webhooks.py": dedent("""
            from shop.http import HttpClient


            def run():
                client = HttpClient()
                for hook in ("created", "paid", "shipped"):
                    client.post(f"/hooks/{hook}", {"event": hook})
                return client.sent
            """),
    }
    shapes = [
        """
        from shop.http import HttpClient
        from shop.ledger import Ledger


        def run():
            ledger = Ledger()
            client = HttpClient()
            ledger.post("acct-{n}", {n})
            client.post("/notify/{n}", {{"n": {n}}})
            return ledger.entries, client.sent
        """,
        """
        from shop.http import HttpClient
        from shop.ledger import Ledger


        class Service{n}:
            def __init__(self):
                self.ledger = Ledger()
                self.client = HttpClient()

            def charge(self, amount):
                self.client.post("/charge", amount)
                return self.ledger.post("acct", amount)


        def run():
            service = Service{n}()
            service.charge({n})
            return service.ledger.entries, service.client.sent
        """,
        """
        from shop.http import HttpClient
        from shop.ledger import Ledger


        def settle(book: Ledger, http: HttpClient, amount: int) -> int:
            http.post("/settle", amount)
            return book.post("settle", amount)


        def run():
            book = Ledger()
            http = HttpClient()
            settle(book, http, {n})
            return book.entries, http.sent
        """,
        """
        from shop import http
        from shop import ledger as ledgers


        def run():
            book = ledgers.Ledger()
            client = http.HttpClient()
            book.post("refund-{n}", -{n})
            client.post("/refund", {n})
            return book.entries, client.sent
        """,
    ]
    for n in range(1, 21):
        files[f"shop/billing/svc_{n:02d}.py"] = dedent(shapes[n % len(shapes)].format(n=n))
    check_py = dedent("""
        import importlib
        import pkgutil
        import sys

        sys.path.insert(0, ".")
        import shop.billing
        import shop.webhooks
        from shop.http import HttpClient
        from shop.ledger import Ledger

        assert hasattr(Ledger, "record") and not hasattr(Ledger, "post")
        assert hasattr(HttpClient, "post") and not hasattr(HttpClient, "record")
        for module in pkgutil.iter_modules(shop.billing.__path__):
            entries, sent = importlib.import_module(f"shop.billing.{module.name}").run()
            assert entries and sent, module.name
        assert len(shop.webhooks.run()) == 3
        print("ok")
        """)
    write_task(
        "py-rename-method",
        "python",
        """
        Rename the method `Ledger.post` in `shop/ledger.py` to `record`. Update every call
        of this method in the project. `HttpClient.post` is a different method: keep it and
        its calls unchanged.
        """,
        files,
        {
            "want": {"shop/ledger.py": ["def record("], "shop/http.py": ["def post("]},
            "wantnot": {"shop/ledger.py": ["def post("], "shop/http.py": ["record"]},
        },
        check='python3 "$VIS_HARBOR_TESTS/check.py"',
        check_py=check_py,
        words=["post", "Ledger", "record"],
    )


# Impact: which handlers reach a function through four layers of calls. Some repositories
# import a decoy function with the same name from another module.

REPO_FNS = ("rows", "count", "latest")
SERVICE_FNS = ("fetch_one", "load_all", "save_one")
SERVICES = ("orders", "users", "stock", "prices", "mail", "audit", "billing", "search")


def impact_model(seed: int, repos: int = 12, handlers: int = 20) -> dict:
    """A random call graph: handlers -> services -> repositories -> db or legacy, with the answer."""
    rng = random.Random(seed)
    calls: dict[tuple, list[tuple]] = {}
    repo_source = {}
    for r in range(repos):
        repo = f"r{r:02d}"
        repo_source[repo] = "legacy" if r % 3 == 2 else "db"
        for fn in REPO_FNS:
            raw = rng.random() < 0.12
            calls[("repo", repo, fn)] = [(repo_source[repo], "raw") if raw else ("db", "execute")]
    for service in SERVICES:
        for fn in SERVICE_FNS:
            picks = rng.sample([f"r{r:02d}" for r in range(repos)], 2)
            calls[("services", service, fn)] = [("repo", repo, rng.choice(REPO_FNS)) for repo in picks]
    for h in range(handlers):
        module = f"h{h:02d}"
        first, second = rng.sample(SERVICES, 2)
        calls[("handlers", module, "get")] = [("services", first, rng.choice(SERVICE_FNS))]
        calls[("handlers", module, "prepare")] = [("services", second, rng.choice(SERVICE_FNS))]
        calls[("handlers", module, "post")] = [("handlers", module, "prepare")]

    def reaches(key, seen=()):
        if key == ("db", "raw"):
            return True
        return any(reaches(c, seen + (key,)) for c in calls.get(key, ()) if c not in seen)

    answer = sorted(
        (m, fn) for (layer, m, fn) in calls if layer == "handlers" and fn != "prepare" and reaches((layer, m, fn))
    )
    assert 0 < len(answer) < 2 * handlers, answer
    return {"calls": calls, "repo_source": repo_source, "repos": repos, "handlers": handlers, "answer": answer}


def _handler_name(lang: str, module: str, fn: str) -> str:
    if lang == "typescript":
        return f"handle{module.capitalize()}{fn.capitalize()}"
    return f"handle-{module}-{fn}" if lang == "clojure" else f"handle_{module}_{fn}"


def _dash(name: str) -> str:
    return name.replace("_", "-")


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def render_impact(lang: str, model: dict) -> tuple[dict[str, str], list[str]]:
    """Source files of an impact model in one language, and the handler names of the answer."""
    calls, files = model["calls"], {}
    repos = [f"r{r:02d}" for r in range(model["repos"])]
    modules = [f"h{h:02d}" for h in range(model["handlers"])]
    if lang == "python":
        files["app/__init__.py"] = ""
        for pkg in ("repo", "services", "handlers"):
            files[f"app/{pkg}/__init__.py"] = ""
        files["app/db.py"] = (
            "def execute(sql, params=()):\n    return [sql, params]\n\n\ndef execute_raw(sql):\n    return [sql]\n"
        )
        files["app/legacy.py"] = (
            '"""Old storage, kept for reports."""\n\n\ndef execute_raw(sql):\n    return ["legacy", sql]\n'
        )
        for repo in repos:
            src = model["repo_source"][repo]
            head = (
                ["from app.db import execute, execute_raw"]
                if src == "db"
                else ["from app.db import execute", "from app.legacy import execute_raw"]
            )
            body = head + [""]
            for fn in REPO_FNS:
                target = "execute_raw" if calls[("repo", repo, fn)][0][1] == "raw" else "execute"
                body += ["", f"def {fn}(key):", f'    return {target}("{repo} {fn} " + str(key))']
            files[f"app/repo/{repo}.py"] = "\n".join(body) + "\n"
        for service in SERVICES:
            used = sorted({c[1] for fn in SERVICE_FNS for c in calls[("services", service, fn)]})
            body = [f"from app.repo import {repo}" for repo in used] + [""]
            for fn in SERVICE_FNS:
                a, b = calls[("services", service, fn)]
                body += ["", f"def {fn}(key):", f"    return {a[1]}.{a[2]}(key) + {b[1]}.{b[2]}(key)"]
            files[f"app/services/{service}.py"] = "\n".join(body) + "\n"
        for m in modules:
            (g,) = calls[("handlers", m, "get")]
            (p,) = calls[("handlers", m, "prepare")]
            alias = f"{p[1]}_{p[2]}"
            body = [
                f"from app.services.{g[1]} import {g[2]}",
                f"from app.services.{p[1]} import {p[2]} as {alias}",
                "",
                "",
                "def _prepare(key):",
                f"    return {alias}(key)",
                "",
                "",
                f"def {_handler_name(lang, m, 'get')}(key):",
                f"    return {g[2]}(key)",
                "",
                "",
                f"def {_handler_name(lang, m, 'post')}(key):",
                "    return _prepare(key)",
            ]
            files[f"app/handlers/{m}.py"] = "\n".join(body) + "\n"
    elif lang == "typescript":
        files["tsconfig.json"] = (
            json.dumps(
                {
                    "compilerOptions": {"strict": True, "noEmit": True, "target": "es2020", "module": "commonjs"},
                    "include": ["src"],
                },
                indent=2,
            )
            + "\n"
        )
        files["src/db.ts"] = (
            "export function execute(sql: string): string[] {\n  return [sql];\n}\n\n"
            'export function executeRaw(sql: string): string[] {\n  return ["raw", sql];\n}\n'
        )
        files["src/legacy.ts"] = (
            '// Old storage, kept for reports.\nexport function executeRaw(sql: string): string[] {\n  return ["legacy", sql];\n}\n'
        )
        for repo in repos:
            src = model["repo_source"][repo]
            head = (
                ['import { execute, executeRaw } from "../db";']
                if src == "db"
                else ['import { execute } from "../db";', 'import { executeRaw } from "../legacy";']
            )
            body = head + [""]
            for fn in REPO_FNS:
                target = "executeRaw" if calls[("repo", repo, fn)][0][1] == "raw" else "execute"
                body += [
                    f"export function {fn}(key: string): string[] {{",
                    f'  return {target}("{repo} {fn} " + key);',
                    "}",
                    "",
                ]
            files[f"src/repo/{repo}.ts"] = "\n".join(body)
        for service in SERVICES:
            used = sorted({c[1] for fn in SERVICE_FNS for c in calls[("services", service, fn)]})
            body = [f'import * as {repo} from "../repo/{repo}";' for repo in used] + [""]
            for fn in SERVICE_FNS:
                a, b = calls[("services", service, fn)]
                body += [
                    f"export function {_camel(fn)}(key: string): string[] {{",
                    f"  return [...{a[1]}.{a[2]}(key), ...{b[1]}.{b[2]}(key)];",
                    "}",
                    "",
                ]
            files[f"src/services/{service}.ts"] = "\n".join(body)
        for m in modules:
            (g,) = calls[("handlers", m, "get")]
            (p,) = calls[("handlers", m, "prepare")]
            alias = f"{p[1]}{_camel(p[2]).capitalize()}"
            imports = [
                f'import {{ {_camel(g[2])} }} from "../services/{g[1]}";',
                f'import {{ {_camel(p[2])} as {alias} }} from "../services/{p[1]}";',
            ]
            if g[1] == p[1] and g[2] == p[2]:
                imports = [f'import {{ {_camel(g[2])}, {_camel(p[2])} as {alias} }} from "../services/{g[1]}";']
            body = imports + [
                "",
                "function prepare(key: string): string[] {",
                f"  return {alias}(key);",
                "}",
                "",
                f"export function {_handler_name(lang, m, 'get')}(key: string): string[] {{",
                f"  return {_camel(g[2])}(key);",
                "}",
                "",
                f"export function {_handler_name(lang, m, 'post')}(key: string): string[] {{",
                "  return prepare(key);",
                "}",
                "",
            ]
            files[f"src/handlers/{m}.ts"] = "\n".join(body)
    elif lang == "clojure":
        files["deps.edn"] = '{:paths ["src"]}\n'
        files["src/shop/db.clj"] = "(ns shop.db)\n\n(defn execute [sql] [sql])\n\n(defn raw-query [sql] [:raw sql])\n"
        files["src/shop/legacy.clj"] = (
            '(ns shop.legacy\n  "Old storage, kept for reports.")\n\n(defn raw-query [sql] [:legacy sql])\n'
        )
        for repo in repos:
            src = model["repo_source"][repo]
            req = (
                "[shop.db :refer [execute raw-query]]"
                if src == "db"
                else "[shop.db :refer [execute]]\n            [shop.legacy :refer [raw-query]]"
            )
            body = [f"(ns shop.repo.{repo}\n  (:require {req}))"]
            for fn in REPO_FNS:
                target = "raw-query" if calls[("repo", repo, fn)][0][1] == "raw" else "execute"
                body += ["", f'(defn {fn} [k]\n  ({target} (str "{repo} {fn} " k)))']
            files[f"src/shop/repo/{repo}.clj"] = "\n".join(body) + "\n"
        for service in SERVICES:
            used = sorted({c[1] for fn in SERVICE_FNS for c in calls[("services", service, fn)]})
            req = "\n            ".join(f"[shop.repo.{repo} :as {repo}]" for repo in used)
            body = [f"(ns shop.services.{service}\n  (:require {req}))"]
            for fn in SERVICE_FNS:
                a, b = calls[("services", service, fn)]
                body += ["", f"(defn {_dash(fn)} [k]\n  (concat ({a[1]}/{a[2]} k) ({b[1]}/{b[2]} k)))"]
            files[f"src/shop/services/{service}.clj"] = "\n".join(body) + "\n"
        for m in modules:
            (g,) = calls[("handlers", m, "get")]
            (p,) = calls[("handlers", m, "prepare")]
            if g[1] == p[1]:
                req = f"[shop.services.{g[1]} :as {p[1]} :refer [{_dash(g[2])}]]"
            else:
                req = f"[shop.services.{g[1]} :refer [{_dash(g[2])}]]\n            [shop.services.{p[1]} :as {p[1]}]"
            body = [
                f"(ns shop.handlers.{m}\n  (:require {req}))",
                "",
                f"(defn- prepare [k]\n  ({p[1]}/{_dash(p[2])} k))",
                "",
                f"(defn {_handler_name(lang, m, 'get')} [k]\n  ({_dash(g[2])} k))",
                "",
                f"(defn {_handler_name(lang, m, 'post')} [k]\n  (prepare k))",
            ]
            files[f"src/shop/handlers/{m}.clj"] = "\n".join(body) + "\n"
    return files, sorted(_handler_name(lang, m, fn) for m, fn in model["answer"])


IMPACT = {
    "python": ("py-impact", "`app.db.execute_raw`", "`app/handlers/`", "handle_h00_get", ["execute_raw", "execute"]),
    "typescript": (
        "ts-impact",
        "`executeRaw` in `src/db.ts`",
        "`src/handlers/`",
        "handleH00Get",
        ["executeRaw", "execute"],
    ),
    "clojure": (
        "clj-impact",
        "`shop.db/raw-query`",
        "`src/shop/handlers/`",
        "handle-h00-get",
        ["raw-query", "execute"],
    ),
}


def impact_task(lang: str) -> None:
    name, target, folder, example, words = IMPACT[lang]
    files, answer = render_impact(lang, impact_model(7))
    decoy = {
        "python": "`app.legacy.execute_raw`",
        "typescript": "`executeRaw` in `src/legacy.ts`",
        "clojure": "`shop.legacy/raw-query`",
    }[lang]
    write_task(
        name,
        lang,
        f"""
        Find the public handler functions in {folder} that can call {target},
        directly or through other functions. {decoy} is a different function: calls of it do
        not count. Write the handler names as a sorted JSON list of strings to `impact.json`
        in the project root, for example `["{example}"]`. Do not change the other files.
        """,
        files,
        {"want": {"impact.json": [example[:6]]}},
        check='python3 "$VIS_HARBOR_TESTS/check.py"',
        check_py=f'import json\n\nassert json.load(open("impact.json")) == {answer!r}\nprint("ok")\n',
        words=words,
    )


def py_impact():
    impact_task("python")


def ts_impact():
    impact_task("typescript")


def clj_impact():
    impact_task("clojure")


# Go: add a parameter to a function and update its calls; a function and a method with
# the same name must stay.


def go_add_param():
    files = {
        "go.mod": "module example.com/shop\n\ngo 1.21\n",
        "pricing/pricing.go": dedent("""
            package pricing

            // Total adds the item prices.
            func Total(items []int) int {
            	sum := 0
            	for _, item := range items {
            		sum += item
            	}
            	return sum
            }
            """),
        "audit/audit.go": dedent("""
            package audit

            // Total counts the audited items.
            func Total(items []int) int {
            	return len(items)
            }
            """),
        "cart/cart.go": dedent("""
            package cart

            import "example.com/shop/pricing"

            type Cart struct {
            	Items []int
            }

            // Total is the price of the cart.
            func (c *Cart) Total() int {
            	return pricing.Total(c.Items)
            }
            """),
    }
    runs = []
    calls = 0
    for i in range(1, 13):
        name = f"c{i:02d}"
        kind = i % 4
        if kind == 0:
            imports, body = '"example.com/shop/pricing"', f"\treturn pricing.Total([]int{{{i}, 2}})"
            calls += 1
        elif kind == 1:
            imports = 'p "example.com/shop/pricing"\n\t"example.com/shop/audit"'
            body = f"\tn := audit.Total([]int{{{i}}})\n\treturn p.Total([]int{{{i}, n}}) + audit.Total(nil)"
            calls += 1
        elif kind == 2:
            imports = '"example.com/shop/cart"\n\t"example.com/shop/pricing"'
            body = f"\tc := cart.Cart{{Items: []int{{{i}}}}}\n\treturn c.Total() + pricing.Total(c.Items)"
            calls += 1
        else:
            imports = '"example.com/shop/cart"\n\t"example.com/shop/audit"'
            body = f"\tc := &cart.Cart{{Items: []int{{{i}, {i}}}}}\n\treturn c.Total() * audit.Total(c.Items)"
        files[f"checkout/{name}.go"] = (
            f"package checkout\n\nimport (\n\t{imports}\n)\n\nfunc Run{i:02d}() int {{\n{body}\n}}\n"
        )
        runs.append(f"Run{i:02d}")
    lines = "\n".join(f"\ttotal += checkout.{run}()" for run in runs)
    files["cmd/shop/main.go"] = (
        f'package main\n\nimport (\n\t"fmt"\n\n\t"example.com/shop/checkout"\n)\n\nfunc main() {{\n\ttotal := 0\n{lines}\n\tfmt.Println("total", total)\n}}\n'
    )
    check_py = dedent(f"""
        import pathlib
        import re

        calls = 0
        for path in pathlib.Path(".").rglob("*.go"):
            text = path.read_text()
            calls += len(re.findall(r'\\b(?:pricing|p)\\.Total\\([^()]*(?:\\([^()]*\\)[^()]*)*, "EUR"\\)', text))
            assert "audit.Total(" not in text or ', "EUR")' not in "".join(re.findall(r"audit\\.Total\\([^)]*\\)", text)), path
        assert calls == {calls + 1}, calls
        assert "func Total(items []int, currency string) int" in pathlib.Path("pricing/pricing.go").read_text()
        assert "func Total(items []int) int" in pathlib.Path("audit/audit.go").read_text()
        assert "func (c *Cart) Total() int" in pathlib.Path("cart/cart.go").read_text()
        print("ok")
        """)
    write_task(
        "go-add-param",
        "go",
        """
        Add a second parameter `currency string` to the function `Total` in
        `pricing/pricing.go`. Pass `"EUR"` as this argument at every call of it. `audit.Total`
        and the method `Cart.Total` are different functions: keep them and their calls
        unchanged.
        """,
        files,
        {"want": {"pricing/pricing.go": ["currency string"]}},
        check='go vet ./... && go run ./cmd/shop && python3 "$VIS_HARBOR_TESTS/check.py"',
        check_py=check_py,
        words=["Total", "currency"],
    )


def ts_rename_export():
    files = {
        "tsconfig.json": json.dumps(
            {
                "compilerOptions": {"strict": True, "noEmit": True, "target": "es2020", "module": "commonjs"},
                "include": ["src"],
            },
            indent=2,
        )
        + "\n",
        "src/money/format.ts": dedent("""
            export const CURRENCY = "EUR";

            export function formatPrice(cents: number): string {
              return `${(cents / 100).toFixed(2)} ${CURRENCY}`;
            }
            """),
        "src/money/index.ts": 'export { formatPrice, CURRENCY } from "./format";\n',
        "src/ui/labels.ts": dedent("""
            export const LABELS = {
              formatPrice: "Price format",
              total: "Total",
            };
            """),
        "src/legacy/report.ts": dedent("""
            function formatPrice(value: number): string {
              return `$${value}`;
            }

            export function legacyReport(values: number[]): string {
              return values.map((value) => formatPrice(value)).join(", ");
            }
            """),
    }
    shapes = [
        """
        import {{ formatPrice }} from "../money/format";

        export function run{n}(): string {{
          return formatPrice({n}00);
        }}
        """,
        """
        import {{ formatPrice as price }} from "../money/format";

        export function run{n}(): string {{
          return price({n}50);
        }}
        """,
        """
        import * as money from "../money/format";
        import {{ LABELS }} from "../ui/labels";

        export function run{n}(): string {{
          return `${{LABELS.formatPrice}}: ${{money.formatPrice({n})}}`;
        }}
        """,
        """
        import {{ formatPrice, CURRENCY }} from "../money";

        export function run{n}(): string {{
          const all = [{n}, {n}0].map(formatPrice);
          return `${{CURRENCY}} ${{all.join(" ")}}`;
        }}
        """,
    ]
    names = []
    for n in range(1, 19):
        files[f"src/features/f{n:02d}.ts"] = dedent(shapes[n % len(shapes)].format(n=n))
        names.append(n)
    main = [f'import {{ run{n} }} from "./features/f{n:02d}";' for n in names]
    main += ['import { legacyReport } from "./legacy/report";', ""]
    main += ["export const results: string[] = ["]
    main += [f"  run{n}()," for n in names] + ["  legacyReport([1, 2]),", "];"]
    files["src/main.ts"] = "\n".join(main) + "\n"
    legacy = files["src/legacy/report.ts"]
    labels = files["src/ui/labels.ts"]
    check_py = dedent(f"""
        import pathlib
        import re

        src = pathlib.Path("src")
        assert "export function formatMoney(" in (src / "money/format.ts").read_text()
        assert (src / "legacy/report.ts").read_text() == {legacy!r}
        assert (src / "ui/labels.ts").read_text() == {labels!r}
        for path in src.rglob("*.ts"):
            if path.name in ("report.ts", "labels.ts") or "misc" in path.parts:
                continue
            text = re.sub(r"LABELS\\.formatPrice", "", path.read_text())
            assert "formatPrice" not in text, path
        print("ok")
        """)
    write_task(
        "ts-rename-export",
        "typescript",
        """
        Rename the exported function `formatPrice` in `src/money/format.ts` to `formatMoney`.
        Update every import, re-export and use of it in `src/`. Keep the local function in
        `src/legacy/report.ts` and the `LABELS.formatPrice` key unchanged. Keep import
        aliases such as `as price`.
        """,
        files,
        {
            "want": {"src/money/format.ts": ["export function formatMoney("]},
            "wantnot": {"src/money/format.ts": ["formatPrice"], "src/money/index.ts": ["formatPrice"]},
        },
        check='tsc -p . && python3 "$VIS_HARBOR_TESTS/check.py"',
        check_py=check_py,
        words=["formatPrice"],
    )


# Go: rename a type that another package also defines.


def go_rename_type():
    files = {
        "go.mod": "module example.com/shop\n\ngo 1.21\n",
        "orders/order.go": dedent("""
            package orders

            type Order struct {
            	ID    string
            	Total int
            }

            func NewOrder(id string, total int) *Order {
            	return &Order{ID: id, Total: total}
            }

            func (o *Order) Add(amount int) {
            	o.Total += amount
            }
            """),
        "orders/store.go": dedent("""
            package orders

            var cache = map[string]*Order{}

            func Save(order *Order) {
            	cache[order.ID] = order
            }

            func Find(id string) (Order, bool) {
            	order, ok := cache[id]
            	if !ok {
            		return Order{}, false
            	}
            	return *order, true
            }
            """),
        "audit/audit.go": dedent("""
            package audit

            import "fmt"

            type Order struct {
            	Ref string
            }

            func Track(order Order) string {
            	return fmt.Sprintf("audit:%s", order.Ref)
            }
            """),
        "billing/receipt.go": dedent("""
            package billing

            import "example.com/shop/orders"

            type Receipt struct {
            	Order orders.Order
            	Note  string
            }

            func NewReceipt(order orders.Order) Receipt {
            	return Receipt{Order: order, Note: "paid"}
            }

            func (r Receipt) Total() int {
            	return r.Order.Total
            }
            """),
    }
    shapes = [
        """
        package billing

        import "example.com/shop/orders"

        func Charge{n}() int {{
        	order := orders.NewOrder("o{n}", {n})
        	order.Add(1)
        	orders.Save(order)
        	return order.Total
        }}
        """,
        """
        package billing

        import (
        	"example.com/shop/audit"
        	"example.com/shop/orders"
        )

        func Batch{n}() []orders.Order {{
        	batch := []orders.Order{{{{ID: "b{n}", Total: {n}}}}}
        	_ = audit.Track(audit.Order{{Ref: "b{n}"}})
        	return batch
        }}
        """,
        """
        package billing

        import "example.com/shop/orders"

        func Index{n}(items map[string]orders.Order) int {{
        	total := 0
        	for _, item := range items {{
        		total += NewReceipt(item).Total()
        	}}
        	return total + {n}
        }}
        """,
        """
        package billing

        import (
        	"example.com/shop/audit"
        	"example.com/shop/orders"
        )

        type Line{n} struct {{
        	Source *orders.Order
        	Trace  audit.Order
        }}

        func Lookup{n}() (*orders.Order, string) {{
        	found, ok := orders.Find("o{n}")
        	if !ok {{
        		found = orders.Order{{ID: "missing"}}
        	}}
        	line := Line{n}{{Source: &found, Trace: audit.Order{{Ref: found.ID}}}}
        	return line.Source, audit.Track(line.Trace)
        }}
        """,
    ]
    main = [
        "package main",
        "",
        "import (",
        '\t"fmt"',
        "",
        '\t"example.com/shop/billing"',
        '\t"example.com/shop/orders"',
        ")",
        "",
        "func main() {",
        "\ttotal := 0",
    ]
    for n in range(1, 13):
        kind = n % len(shapes)
        files[f"billing/b{n:02d}.go"] = dedent(shapes[kind].format(n=n))
        main.append(
            [
                f"\ttotal += billing.Charge{n}()",
                f"\ttotal += len(billing.Batch{n}())",
                f'\ttotal += billing.Index{n}(map[string]orders.Order{{"a": {{Total: 1}}}})',
                f"\tif found, _ := billing.Lookup{n}(); found != nil {{\n\t\ttotal++\n\t}}",
            ][kind]
        )
    main += ['\tfmt.Println("total", total)', "}"]
    files["cmd/shop/main.go"] = "\n".join(main) + "\n"
    audit = files["audit/audit.go"]
    check_py = dedent(f"""
        import pathlib
        import re

        orders = pathlib.Path("orders/order.go").read_text()
        assert "type Purchase struct" in orders and "type Order struct" not in orders
        assert "func NewOrder(" in orders
        assert pathlib.Path("audit/audit.go").read_text() == {audit!r}
        receipt = pathlib.Path("billing/receipt.go").read_text()
        assert re.search(r"\\bOrder\\s+orders\\.Purchase", receipt), receipt
        assert "r.Order.Total" in receipt
        for path in pathlib.Path(".").rglob("*.go"):
            assert "orders.Order" not in path.read_text(), path
        print("ok")
        """)
    write_task(
        "go-rename-type",
        "go",
        """
        Rename the Go type `Order` of package `orders` to `Purchase`. Update every use of
        it in the module. Keep the constructor name `NewOrder`, struct field names and the
        separate type `audit.Order` unchanged.
        """,
        files,
        {"want": {"orders/order.go": ["type Purchase struct", "func NewOrder("]}},
        check='go vet ./... && go run ./cmd/shop && python3 "$VIS_HARBOR_TESTS/check.py"',
        check_py=check_py,
        words=["Order"],
    )


# Rust: rename a free function that shares its name with a method.


def rs_rename_fn():
    files = {
        "Cargo.toml": '[package]\nname = "shop"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
        "src/util.rs": dedent("""
            pub fn checksum(data: &[u8]) -> u32 {
                data.iter().fold(0u32, |acc, byte| acc.wrapping_mul(31).wrapping_add(*byte as u32))
            }
            """),
        "src/packet.rs": dedent("""
            use crate::util;

            pub struct Packet {
                pub bytes: Vec<u8>,
            }

            impl Packet {
                pub fn new(bytes: &[u8]) -> Self {
                    Packet { bytes: bytes.to_vec() }
                }

                pub fn checksum(&self) -> u32 {
                    util::checksum(&self.bytes)
                }
            }
            """),
    }
    shapes = [
        """
        use crate::util::checksum;

        pub fn run() -> u32 {{
            checksum(b"net{n}")
        }}
        """,
        """
        use crate::packet::Packet;
        use crate::util;

        pub fn run() -> u32 {{
            let packet = Packet::new(b"pkt{n}");
            packet.checksum() ^ util::checksum(&packet.bytes)
        }}
        """,
        """
        use crate::util::{{self, checksum as sum}};

        pub fn run() -> u32 {{
            sum(b"alias{n}").wrapping_add(util::checksum(b"{n}"))
        }}
        """,
        """
        use crate::packet::Packet;

        pub fn run() -> u32 {{
            let packets = [Packet::new(b"a{n}"), Packet::new(b"b{n}")];
            packets.iter().map(|packet| packet.checksum()).sum::<u32>()
                ^ crate::util::checksum(b"full{n}")
        }}
        """,
    ]
    mods = []
    for n in range(1, 13):
        files[f"src/net/n{n:02d}.rs"] = dedent(shapes[n % len(shapes)].format(n=n))
        mods.append(f"n{n:02d}")
    files["src/net/mod.rs"] = "".join(f"pub mod {m};\n" for m in mods)
    main = ["mod misc;", "mod net;", "mod packet;", "mod util;", "", "fn main() {", "    let mut total = 0u32;"]
    main += [f"    total ^= net::{m}::run();" for m in mods]
    main += ['    println!("total {total}");', "}"]
    files["src/main.rs"] = "\n".join(main) + "\n"
    method_calls = sum(text.count(".checksum()") for text in files.values())
    check_py = dedent(f"""
        import pathlib

        src = pathlib.Path("src")
        assert "pub fn crc32(" in (src / "util.rs").read_text()
        packet = (src / "packet.rs").read_text()
        assert "pub fn checksum(&self)" in packet and "util::crc32(" in packet
        texts = [path.read_text() for path in src.rglob("*.rs")]
        assert sum(text.count(".checksum()") for text in texts) == {method_calls}
        for text in texts:
            assert "util::checksum" not in text and "checksum as" not in text
        print("ok")
        """)
    write_task(
        "rs-rename-fn",
        "rust",
        """
        Rename the free function `checksum` in `src/util.rs` to `crc32`. Update every call
        and `use` of it in the crate. The method `Packet::checksum` is a different function:
        keep it and its calls unchanged. Keep import aliases such as `as sum`.
        """,
        files,
        {"want": {"src/util.rs": ["pub fn crc32("]}, "wantnot": {"src/util.rs": ["checksum"]}},
        check=(
            'CARGO_TARGET_DIR="$VIS_HARBOR_VERIFIER_LOGS/target" cargo run -q --offline'
            ' && python3 "$VIS_HARBOR_TESTS/check.py"'
        ),
        check_py=check_py,
        words=["checksum"],
    )


# Clojure: rename a var that another namespace also defines.


def clj_rename_var():
    files = {
        "deps.edn": '{:paths ["src"]}\n',
        "src/shop/pricing.clj": dedent("""
            (ns shop.pricing)

            (defn discount
              "Return the price after a percent discount."
              [price pct]
              (- price (quot (* price pct) 100)))
            """),
        "src/shop/coupons.clj": dedent("""
            (ns shop.coupons)

            (def codes {"SPRING" 10 "VIP" 25})

            (defn discount
              "Return the percent of a coupon code."
              [code]
              (get codes code 0))
            """),
    }
    shapes = [
        """
        (ns shop.orders.o{n}
          (:require [shop.pricing :as pricing]))

        (defn run []
          (pricing/discount {n}00 10))
        """,
        """
        (ns shop.orders.o{n}
          (:require [shop.coupons :as coupons]
                    [shop.pricing :refer [discount]]))

        (defn run []
          (discount {n}00 (coupons/discount "VIP")))
        """,
        """
        (ns shop.orders.o{n}
          (:require [shop.pricing :as p]))

        (def ^:private apply-all (partial map #(p/discount % 5)))

        (defn run []
          (reduce + (apply-all [{n} {n}0 {n}00])))
        """,
        """
        (ns shop.orders.o{n}
          (:require [shop.coupons :refer [discount]]
                    [shop.pricing :as pricing]))

        (defn run []
          (let [f #'pricing/discount]
            (f {n}00 (discount "SPRING"))))
        """,
    ]
    nss = []
    for n in range(1, 13):
        files[f"src/shop/orders/o{n}.clj"] = dedent(shapes[n % len(shapes)].format(n=n))
        nss.append(f"shop.orders.o{n}")
    requires = "\n            ".join(f"[{ns}]" for ns in nss)
    calls = " ".join(f"({ns}/run)" for ns in nss)
    files["src/shop/main.clj"] = (
        f'(ns shop.main\n  (:require {requires}))\n\n(defn -main [& _]\n  (println "total" (+ {calls})))\n'
    )
    coupons = files["src/shop/coupons.clj"]
    check_py = dedent(f"""
        import pathlib
        import re

        src = pathlib.Path("src/shop")
        assert "(defn apply-discount" in (src / "pricing.clj").read_text()
        assert (src / "coupons.clj").read_text() == {coupons!r}
        for path in (src / "orders").glob("*.clj"):
            text = path.read_text()
            assert not re.search(r"(pricing|\\bp)/discount\\b", text), path
            if "shop.pricing :refer" in text:
                assert "[apply-discount]" in text, path
        print("ok")
        """)
    write_task(
        "clj-rename-var",
        "clojure",
        """
        Rename the var `shop.pricing/discount` to `apply-discount`. Update every reference
        to it in `src/`, including `:refer` lists and var quotes. `shop.coupons/discount`
        is a different var: keep it and its references unchanged.
        """,
        files,
        {"want": {"src/shop/pricing.clj": ["(defn apply-discount"]}},
        check='clojure -M -m shop.main && python3 "$VIS_HARBOR_TESTS/check.py"',
        check_py=check_py,
        words=["discount"],
    )


def main():
    shutil.rmtree(TASKS, ignore_errors=True)
    for make in (
        py_rename_method,
        py_impact,
        ts_impact,
        clj_impact,
        ts_rename_export,
        go_rename_type,
        go_add_param,
        rs_rename_fn,
        clj_rename_var,
    ):
        make()
    print("\n".join(sorted(path.name for path in TASKS.iterdir())))


if __name__ == "__main__":
    main()
