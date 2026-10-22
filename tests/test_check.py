import subprocess

from vis_graphify import tools as t


def _runner(code, out=""):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    return run, calls


def _which(monkeypatch, *names):
    monkeypatch.setattr(t.shutil, "which", lambda name: f"/bin/{name}" if name in names else None)


def test_go_check_reports_pass_and_first_errors(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module x\n")
    _which(monkeypatch, "go")
    run, calls = _runner(0)
    assert t.project_check(tmp_path, ["a.go"], run) == "go vet ./...: passed"
    assert calls[0][0] == ["go", "vet", "./..."] and calls[0][1]["cwd"] == str(tmp_path)
    run, _ = _runner(1, "# x\n./a.go:3:2: undefined: Order\n")
    assert t.project_check(tmp_path, ["a.go"], run) == "go vet ./...: failed\n# x\n./a.go:3:2: undefined: Order"


def test_cargo_check_builds_outside_the_project(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    _which(monkeypatch, "cargo")
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    run, calls = _runner(0)
    assert t.project_check(tmp_path, ["src/a.rs"], run) == "cargo check -q: passed"
    assert not calls[0][1]["env"]["CARGO_TARGET_DIR"].startswith(str(tmp_path))


def test_tsc_prefers_the_local_compiler(tmp_path, monkeypatch):
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "node_modules/.bin").mkdir(parents=True)
    (tmp_path / "node_modules/.bin/tsc").write_text("")
    _which(monkeypatch)
    run, calls = _runner(0)
    assert t.project_check(tmp_path, ["a.ts"], run) == "tsc --noEmit -p .: passed"
    assert calls[0][0][0] == str(tmp_path / "node_modules/.bin/tsc")


def test_clj_kondo_fails_only_on_errors(tmp_path, monkeypatch):
    (tmp_path / "deps.edn").write_text("{}")
    (tmp_path / "src").mkdir()
    _which(monkeypatch, "clj-kondo")
    run, calls = _runner(2, "src/a.clj:1:1: warning: unused\n")
    assert t.project_check(tmp_path, ["src/a.clj"], run) == "clj-kondo --lint src: passed"
    out = "src/a.clj:1:1: warning: unused\nsrc/a.clj:4:2: error: Unresolved symbol: discount\n"
    run, _ = _runner(3, out)
    assert t.project_check(tmp_path, ["src/a.clj"], run) == (
        "clj-kondo --lint src: failed\nsrc/a.clj:4:2: error: Unresolved symbol: discount"
    )


def test_python_check_compiles_in_memory(tmp_path, monkeypatch):
    _which(monkeypatch)
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "bad.py").write_text("def f(:\n")
    assert t.project_check(tmp_path, ["ok.py"]) == "python syntax of 1 files: passed"
    assert t.project_check(tmp_path, ["ok.py", "bad.py"]).startswith("python syntax of 2 files: failed\nbad.py:1 ")
    assert not (tmp_path / "__pycache__").exists()
    assert t.project_check(tmp_path, ["a.md"]) == ""


def test_no_check_tool_means_no_check(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module x\n")
    _which(monkeypatch)
    assert t.project_check(tmp_path, ["a.go"], _runner(0)[0]) == ""
