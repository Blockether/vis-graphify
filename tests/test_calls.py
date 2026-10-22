"""Plan parameter and argument insertions from reference sites, without a graph."""

from dataclasses import dataclass

from vis_graphify import calls


@dataclass(frozen=True)
class Site:
    file: str
    start: int
    end: int
    line: int
    role: str


def _sites(text: str, file: str, name: str, roles: list[str]) -> list[Site]:
    found, at = [], text.find(name)
    while at >= 0:
        found.append(Site(file, at, at + len(name), text.count("\n", 0, at) + 1, roles[len(found)]))
        at = text.find(name, at + len(name))
    return found


def _apply(text: str, file: str, name: str, roles: list[str], parameter: str, argument: str) -> tuple[str, tuple]:
    plan = calls.plan(_sites(text, file, name, roles), parameter, argument, {file: text})
    if plan.conflicts:
        return text, plan.conflicts
    return calls.rewrite(plan, {file: text.encode()})[file].decode(), ()


def test_go_definition_and_nested_call():
    text = 'func Total(items []int) int { return 0 }\nvar x = Total(f([]int{1}), g("a)"))\n'
    out, conflicts = _apply(text, "a.go", "Total", ["definition", "use"], "currency string", '"EUR"')
    assert conflicts == ()
    assert "func Total(items []int, currency string) int" in out
    assert 'Total(f([]int{1}), g("a)"), "EUR")' in out


def test_empty_lists_and_typescript_generics():
    text = "export function load<T>(): T[] { return []; }\nconst a = load<number>();\n"
    out, _ = _apply(text, "a.ts", "load", ["definition", "use"], "key: string", "'k'")
    assert "load<T>(key: string)" in out and "load<number>('k')" in out


def test_multiline_call_with_trailing_comma_keeps_its_layout():
    text = "def run(a):\n    pass\n\nrun(\n    1,\n)\n"
    out, _ = _apply(text, "a.py", "run", ["definition", "use"], "b=None", "b=2")
    assert "def run(a, b=None):" in out and "run(\n    1,\n    b=2,\n)" in out


def test_python_lists_that_cannot_take_a_last_positional_item_conflict():
    text = "def run(a, b=1):\n    pass\n\nrun(1, **opts)\n"
    out, conflicts = _apply(text, "a.py", "run", ["definition", "use"], "c", "3")
    assert out == text
    assert any("a.py:1" in c and "default" in c for c in conflicts)
    assert any("a.py:4" in c and "**kwargs" in c for c in conflicts)


def test_clojure_definition_vector_and_call_form():
    text = '(defn total\n  "Adds [items]."\n  [items]\n  (reduce + items))\n\n(total [1 2])\n(p/total [3])\n'
    out, _ = _apply(text, "a.clj", "total", ["definition", "use", "use"], "currency", '"EUR"')
    assert "[items currency]" in out and '(total [1 2] "EUR")' in out and '(p/total [3] "EUR")' in out


def test_a_function_passed_as_a_value_is_a_conflict_and_imports_stay():
    text = "import { run } from './jobs';\nlist.map(run);\n"
    plan = calls.plan(_sites(text, "a.ts", "run", ["import", "use"]), "x: number", "1", {"a.ts": text})
    assert len(plan.conflicts) == 1 and "used as a value" in plan.conflicts[0]
