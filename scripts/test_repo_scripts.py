"""Tests for the repo-scanning scripts (run: python3 -m pytest scripts/test_repo_scripts.py -q).

group 316: a local virtualenv under services/<svc>/ (the Ubuntu VM has one) must never be read as production code,
and the shared-copy hash must not depend on the Python version.
"""
from __future__ import annotations

import ast
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import _repo_walk  # noqa: E402


def _mini_repo(tmp: Path) -> Path:
    """A throw-away repo with one real service file and a fake venv full of third-party env reads."""
    (tmp / "scripts").mkdir()
    for f in ("_repo_walk.py", "gen_env_reference.py", "check_price_source_imports.py"):
        shutil.copy(SCRIPTS / f, tmp / "scripts" / f)
    (tmp / "docs").mkdir()
    svc = tmp / "services" / "demo-service"
    svc.mkdir(parents=True)
    (svc / "main.py").write_text('import os\nX = os.getenv("REAL_FLAG", "1")\n')
    (svc / "tests").mkdir()
    (svc / "tests" / "test_x.py").write_text('import os\nos.getenv("TEST_ONLY")\n')
    venv = svc / ".venv" / "lib" / "python3.12" / "site-packages" / "coverage"
    venv.mkdir(parents=True)
    (svc / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "config.py").write_text('import os\nos.getenv("COVERAGE_CORE")\nimport yfinance\n')
    odd = svc / "myenv"  # a venv with an unusual name: recognised by pyvenv.cfg
    odd.mkdir()
    (odd / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (odd / "mod.py").write_text('import os\nos.getenv("ODD_VENV_VAR")\n')
    return tmp


def test_is_vendored_dir(tmp_path):
    for name in (".venv", "venv", "site-packages", "__pycache__", "node_modules", "foo.egg-info", "env"):
        (tmp_path / name).mkdir()
        assert _repo_walk.is_vendored_dir(tmp_path / name), name
    (tmp_path / "market_data").mkdir()
    assert not _repo_walk.is_vendored_dir(tmp_path / "market_data")
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "pyvenv.cfg").write_text("")
    assert _repo_walk.is_vendored_dir(tmp_path / "custom")


def test_env_reference_ignores_virtualenv(tmp_path):
    repo = _mini_repo(tmp_path)
    r = subprocess.run([sys.executable, str(repo / "scripts" / "gen_env_reference.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = (repo / "docs" / "ENV_REFERENCE.md").read_text()
    assert "`REAL_FLAG`" in text
    for leaked in ("COVERAGE_CORE", "ODD_VENV_VAR", "TEST_ONLY"):
        assert leaked not in text
    assert "1 variables are read" in text
    chk = subprocess.run([sys.executable, str(repo / "scripts" / "gen_env_reference.py"), "--check"], capture_output=True, text=True)
    assert chk.returncode == 0, chk.stdout


def test_price_source_check_ignores_virtualenv(tmp_path):
    repo = _mini_repo(tmp_path)
    r = subprocess.run([sys.executable, str(repo / "scripts" / "check_price_source_imports.py"), "--list"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "", r.stdout


def test_canonical_hash_ignores_version_only_fields():
    import check_shared_copies as c
    importlib.reload(c)
    fn = ast.parse("def f(a):\n    return a\n").body[0]
    base = c._canon(fn)
    fn.type_params = []          # what 3.12 adds
    fn.decorator_list = []       # what 3.13 may stop printing
    assert c._canon(fn) == base
    other = ast.parse("def f(a):\n    return a + 1\n").body[0]
    assert c._canon(other) != base


def test_checked_in_generated_files_are_current():
    """The committed ENV_REFERENCE.md and shared-copy baseline match the code (same as the CI steps)."""
    for cmd in (["gen_env_reference.py", "--check"], ["check_shared_copies.py"]):
        r = subprocess.run([sys.executable, str(SCRIPTS / cmd[0]), *cmd[1:]], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


# ---- group341c: merge-conflict marker check ----------------------------------------------------------------------------
def test_conflict_scan_finds_markers_even_when_indented():
    import check_merge_conflicts as c
    text = "def f():\n    if x:\n    <<<<<<< HEAD\n        a = 1\n    =======\n        a = 2\n    >>>>>>> origin/main\n"
    assert c.scan_text(text) == [(3, "<<<<<<<"), (5, "======="), (7, ">>>>>>>")]


def test_conflict_scan_ignores_underlines_and_headings():
    import check_merge_conflicts as c
    assert c.scan_text('"""Title\n=================\nbody\n"""\nx = "<<<<<<<<"  # 8 chars inside text\nSetext\n=======\n') == []
    assert c.scan_text("a <<<<<<< b\n") == []        # not at the start of a line


def test_conflict_check_cli(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text("x = 1\n")
    bad = tmp_path / "svc" / "reconcile.py"
    bad.parent.mkdir()
    bad.write_text("if True:\n<<<<<<< HEAD\n    pass\n=======\n    pass\n>>>>>>> branch\n")
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "mod.py").write_text("<<<<<<< HEAD\n")             # vendored: ignored
    r = subprocess.run([sys.executable, str(SCRIPTS / "check_merge_conflicts.py"), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 1
    assert "reconcile.py:2" in r.stdout and "mod.py" not in r.stdout
    r2 = subprocess.run([sys.executable, str(SCRIPTS / "check_merge_conflicts.py"), str(ok)], capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout


def test_repo_has_no_conflict_markers():
    r = subprocess.run([sys.executable, str(SCRIPTS / "check_merge_conflicts.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_every_service_dockerfile_compiles_code_at_build_time():
    """A syntax error must fail `docker compose build`, not crash-loop the container (the 2026-10-13 reconcile.py case)."""
    for df in sorted((ROOT / "services").glob("*/Dockerfile")):
        text = df.read_text()
        assert "compileall" in text, f"{df.parent.name}: no compileall step"
        # group442: calibration-runner-service builds from the repo root, so it copies named files / folders instead of `COPY . .`; compileall must still come after all COPY lines
        assert "COPY" in text and text.rindex("\nCOPY ") < text.index("compileall"), f"{df.parent.name}: compileall must run after the last COPY"


def test_compileall_skips_any_virtualenv_folder_and_dockerignore_drops_them():
    """A VM test venv named .venv-test (Python 3.14) was copied into the image by `COPY . .`; compileall then died on 3.14-only syntax in numpy and the build failed."""
    import re
    rx = re.compile(r"(^|/)(tests?|\.venv[^/]*|venv|node_modules)(/|$)")
    assert rx.search("./.venv-test/lib/python3.14/site-packages/numpy/_typing/_shape.py")
    assert not rx.search("./orders/reconcile.py")
    for df in sorted((ROOT / "services").glob("*/Dockerfile")):
        assert r"\.venv[^/]*" in df.read_text(), f"{df.parent.name}: compileall does not skip .venv-* folders"
        di = df.parent / ".dockerignore"
        if di.exists():
            assert "**/.venv*" in di.read_text(), f"{df.parent.name}: .dockerignore does not drop .venv-* folders"
    assert "**/.venv*" in (ROOT / ".dockerignore").read_text()
