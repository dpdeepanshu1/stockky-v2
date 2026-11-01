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
    text = (repo / "docs" / "ENV_REFERENCE.txt").read_text()
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
    """The committed ENV_REFERENCE.txt and shared-copy baseline match the code (same as the CI steps)."""
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


def test_env_reference_reads_wrapper_and_indirect_calls(tmp_path):
    """E170: names read through `__import__("os").getenv`, `ref.os.getenv` and small wrappers such as `_env_int("X_Y", 5)` are listed."""
    repo = _mini_repo(tmp_path)
    (repo / "services" / "demo-service" / "more.py").write_text(
        'X1 = int(__import__("os").getenv("DIRECT_INDIRECT_ONE", "7"))\n'
        'X2 = ref.os.getenv("ATTR_PATH_TWO")\n'
        'X3 = _env_int("WRAPPED_THREE", 5)\n'
        'X4 = _bs_bool("WRAPPED_FOUR", "true")\n'
        'X5 = logger.info("NOT_A_VARIABLE")\n'
        'X6 = _env_int("lowercase_name", 1)\n'
    )
    r = subprocess.run([sys.executable, str(repo / "scripts" / "gen_env_reference.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = (repo / "docs" / "ENV_REFERENCE.txt").read_text()
    for name in ("DIRECT_INDIRECT_ONE", "ATTR_PATH_TWO", "WRAPPED_THREE", "WRAPPED_FOUR", "REAL_FLAG"):
        assert f"`{name}`" in text, name
    assert "`WRAPPED_THREE` (helper)" in text and "`DIRECT_INDIRECT_ONE` (helper)" not in text
    assert "NOT_A_VARIABLE" not in text and "lowercase_name" not in text


def test_service_sheet_tool_covers_every_area():
    """E170: the sheet tool measures every area. (E245: the hand-written docs/SERVICE_SHEET_<area>.md files were folded into
    CHANGELOG.md and removed - the repo keeps only README.md and CHANGELOG.md - so the test no longer expects them.)"""
    import audit_service_sheet as t
    assert len(t.AREAS) == 12
    m = t.measure("kronos-forecast-service")
    assert m["files"] > 0 and m["funcs"] > 0 and m["test_files"] > 0
    row = t.md(m)
    assert row.count("\n") >= 6 and "| 6 | Security |" in row


# ── E174: stale tracked files and the env-reference diff ─────────────────────────────────────────────────────────────

def test_stale_files_lists_tracked_files_missing_from_the_manifest(tmp_path):
    import check_stale_files as csf
    assert csf.stale(["a.py", "old/TabButton.tsx", "b.md"], {"a.py", "b.md"}) == ["old/TabButton.tsx"]
    assert csf.stale(["a.py"], {"a.py", "new.py"}) == []


def test_stale_files_script_in_a_real_git_repo(tmp_path):
    repo = tmp_path / "r"
    (repo / "scripts").mkdir(parents=True)
    (repo / "docs").mkdir()
    shutil.copy(SCRIPTS / "check_stale_files.py", repo / "scripts" / "check_stale_files.py")
    (repo / "keep.txt").write_text("k")
    (repo / "gone.txt").write_text("g")
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)
    run("git", "init", "-q")
    (repo / "gone.txt").unlink()                       # the release no longer has it, git still tracks it below
    run("git", "add", "-A")
    (repo / "gone.txt").write_text("g")
    run("git", "add", "gone.txt")
    assert run(sys.executable, "scripts/check_stale_files.py", "--write").returncode == 0   # manifest = files now on disk
    (repo / "gone.txt").unlink()
    (repo / "docs" / "FILE_MANIFEST.txt").write_text("keep.txt\ndocs/FILE_MANIFEST.txt\nscripts/check_stale_files.py\n")
    r = run(sys.executable, "scripts/check_stale_files.py")
    assert r.returncode == 1 and "git rm -f -- 'gone.txt'" in r.stdout and "keep.txt'" not in r.stdout


def test_verify_mode_flags_a_file_missing_from_the_manifest_and_one_that_is_gone(tmp_path):
    """E285 round 96: --verify compares the manifest with the files on disk in BOTH directions (round 95 shipped 7 live files the manifest did not list)."""
    repo = tmp_path / "r"
    (repo / "scripts").mkdir(parents=True)
    (repo / "docs").mkdir()
    shutil.copy(SCRIPTS / "check_stale_files.py", repo / "scripts" / "check_stale_files.py")
    (repo / "a.py").write_text("a")
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)
    assert run(sys.executable, "scripts/check_stale_files.py", "--write").returncode == 0
    ok = run(sys.executable, "scripts/check_stale_files.py", "--verify")
    assert ok.returncode == 0 and "matches the disk" in ok.stdout
    (repo / "new_live_file.py").write_text("x")                      # added to the release, manifest not regenerated
    (repo / "a.py").unlink()                                         # listed, but not in the release
    bad = run(sys.executable, "scripts/check_stale_files.py", "--verify")
    assert bad.returncode == 1 and "new_live_file.py" in bad.stdout and "in the manifest but not on disk: a.py" in bad.stdout and "--write" in bad.stdout


def test_verify_mode_ignores_gitignored_leftovers_but_still_checks_listed_files(tmp_path):
    """E285 round 122: the VM checkout holds .venv-test/ and stockky_test_report_*/ (both in .gitignore, never in a release). --verify listed thousands of them."""
    repo = tmp_path / "r"
    (repo / "scripts").mkdir(parents=True)
    (repo / "docs").mkdir()
    shutil.copy(SCRIPTS / "check_stale_files.py", repo / "scripts" / "check_stale_files.py")
    (repo / ".gitignore").write_text(".venv-test/\nstockky_test_report_*/\n*.md\n")
    (repo / "a.py").write_text("a")
    (repo / "docs" / "kept.md").write_text("k")                      # ignored by *.md but listed in the manifest (like docs/E283_*.md)
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)
    run("git", "init", "-q")
    assert run(sys.executable, "scripts/check_stale_files.py", "--write").returncode == 0
    (repo / ".venv-test" / "lib").mkdir(parents=True)
    (repo / ".venv-test" / "lib" / "x.py").write_text("x")
    (repo / "stockky_test_report_20261101-000000").mkdir()
    (repo / "stockky_test_report_20261101-000000" / "r.log").write_text("l")
    ok = run(sys.executable, "scripts/check_stale_files.py", "--verify")
    assert ok.returncode == 0 and "matches the disk" in ok.stdout, ok.stdout
    (repo / "docs" / "kept.md").unlink()                             # listed file gone: still reported
    (repo / "new_live_file.py").write_text("n")                      # not ignored, not listed: still reported (no file is tracked yet, so the disk-only rules apply)
    bad = run(sys.executable, "scripts/check_stale_files.py", "--verify")
    assert bad.returncode == 1 and "new_live_file.py" in bad.stdout and "docs/kept.md" in bad.stdout and ".venv-test" not in bad.stdout


def test_every_tracked_file_of_this_checkout_is_in_the_manifest():
    """E285 round 96: the same gap seen from CI. A tracked file the manifest does not list makes the VM's check_stale_files print a `git rm` line for live code. Skipped outside a git checkout."""
    import check_stale_files as csf
    tracked = csf.tracked_files(csf.ROOT)
    if not tracked:
        return
    known = {l.strip() for l in csf.MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()}
    gone = csf.stale(tracked, known)
    assert not gone, f"tracked files missing from docs/FILE_MANIFEST.txt (run: python3 scripts/check_stale_files.py --write): {gone[:10]}"


def _git_repo_with_release(tmp_path):
    """E285 round 124: a checkout like the VM's: a tracked release (listed in the manifest) plus untracked local leftovers that .gitignore does NOT cover."""
    repo = tmp_path / "r"
    (repo / "scripts").mkdir(parents=True)
    (repo / "docs").mkdir()
    shutil.copy(SCRIPTS / "check_stale_files.py", repo / "scripts" / "check_stale_files.py")
    (repo / ".gitignore").write_text(".venv-test/\n")
    (repo / "a.py").write_text("a")
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)
    run("git", "init", "-q")
    assert run(sys.executable, "scripts/check_stale_files.py", "--write").returncode == 0      # manifest = the release
    run("git", "add", "-A")
    return repo, run


def test_verify_in_a_git_checkout_summarises_untracked_leftovers_in_one_line(tmp_path):
    """E285 round 124: bars/, hist400/, kronos*.log, rt_*.json ... are untracked and NOT ignored. --verify printed ~10,000 `git rm` lines for them."""
    repo, run = _git_repo_with_release(tmp_path)
    for d, n in (("bars", 30), ("hist400", 50)):
        (repo / d).mkdir()
        for i in range(n):
            (repo / d / f"f{i}.json").write_text("{}")
    (repo / "kronos_full.log").write_text("l")
    (repo / "rt_2026-10-18.json").write_text("{}")
    (repo / ".venv-test").mkdir()
    (repo / ".venv-test" / "x.py").write_text("x")
    r = run(sys.executable, "scripts/check_stale_files.py", "--verify")
    assert r.returncode == 0 and r.stdout.splitlines()[0].startswith("manifest matches the disk: "), r.stdout
    assert "git rm" not in r.stdout and "on disk but not in the manifest" not in r.stdout
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 2 and lines[1].startswith("note: ") and "hist400/ (50)" in lines[1] and "bars/ (30)" in lines[1] and "kronos_full.log" in lines[1], r.stdout


def test_verify_in_a_git_checkout_still_flags_a_tracked_file_the_manifest_lacks_and_a_listed_file_that_is_gone(tmp_path):
    repo, run = _git_repo_with_release(tmp_path)
    (repo / "new_live_file.py").write_text("n")
    run("git", "add", "new_live_file.py")                           # tracked now, manifest not regenerated
    (repo / "a.py").unlink()                                        # listed, gone from disk
    (repo / "bars").mkdir()
    (repo / "bars" / "x.json").write_text("{}")
    r = run(sys.executable, "scripts/check_stale_files.py", "--verify")
    assert r.returncode == 1, r.stdout
    assert "on disk but not in the manifest (the VM would be told to git rm it): new_live_file.py" in r.stdout
    assert "in the manifest but not on disk: a.py" in r.stdout and "bars/x.json" not in r.stdout and "note: 1 untracked" in r.stdout


def test_untracked_summary_groups_by_top_level_folder_and_caps_the_groups():
    import check_stale_files as csf
    paths = [f"d{i}/f.json" for i in range(12)] + ["bars/a.json", "bars/b.json", "x.log"]
    out = csf.untracked_summary(sorted(paths))
    assert out.startswith("note: 15 untracked local file(s)") and "bars/ (2)" in out and "+6 more" in out and out.count("\n") == 0
    assert "x.log" in csf.untracked_summary(["x.log"]) and "x.log (" not in csf.untracked_summary(["x.log"])


def test_the_e283_protocol_note_is_gone_and_no_code_points_at_it():
    """E285 round 124: .gitignore keeps only README.md and CHANGELOG.md, so docs/E283_HALF_SELL_PROBE_PROTOCOL.md could never be in a git checkout (--verify: "in the manifest but not on disk")."""
    name = "E283_HALF_SELL_PROBE_PROTOCOL"
    assert not (ROOT / "docs" / (name + ".md")).exists()
    assert name not in (ROOT / "docs" / "FILE_MANIFEST.txt").read_text(encoding="utf-8")
    for rel in ("scripts/dhan_half_sell_probe.py", "services/position-stocks-service/orders/half_sell_probe.py"):
        assert name not in (ROOT / rel).read_text(encoding="utf-8"), rel
    md = [l for l in (ROOT / "docs" / "FILE_MANIFEST.txt").read_text(encoding="utf-8").splitlines() if l.startswith("docs/") and l.endswith(".md")]
    assert md == [], md                                             # docs/ holds no Markdown note any more (a note there cannot survive a git checkout)


# ── E285 round 124: daily history freshness (fetch_history_for_backtest --max-age-days / --report-ages) ───────────────

def _fh_candles(last, n=300):
    import datetime as dt
    end = dt.date.fromisoformat(last)
    return [{"date": (end - dt.timedelta(days=n - 1 - i)).isoformat(), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1} for i in range(n)]


def test_candle_date_reads_iso_and_epoch_and_is_stale_only_beyond_the_limit():
    import datetime as dt
    import fetch_history_for_backtest as fh
    assert fh.candle_date({"date": "2026-10-30"}) == dt.date(2026, 10, 30)
    assert fh.candle_date({"date": "2026-10-30T00:00:00+05:30"}) == dt.date(2026, 10, 30)
    assert fh.candle_date({"t": 1793318400}) is not None and fh.candle_date({"date": "junk"}) is None and fh.candle_date("x") is None
    today = dt.date(2026, 11, 2)
    assert fh.is_stale(dt.date(2026, 10, 28), 5, today) is False        # 5 days old: a long weekend is not stale
    assert fh.is_stale(dt.date(2026, 10, 27), 5, today) is True
    assert fh.is_stale(dt.date(2020, 1, 1), 0, today) is False          # off by default
    assert fh.is_stale(None, 5, today) is True


def test_a_rerun_fetches_a_stale_file_again_and_keeps_the_old_one_when_the_refetch_fails_or_is_not_newer(tmp_path, monkeypatch, capsys):
    import json
    import fetch_history_for_backtest as fh
    out = tmp_path / "h"
    out.mkdir()
    old = _fh_candles("2026-10-20")
    for sym in ("OLDOK", "OLDFAIL", "OLDSAME", "FRESH"):
        last = "2026-10-30" if sym == "FRESH" else "2026-10-20"
        (out / f"{sym}.json").write_text(json.dumps({"symbol": sym, "candles": _fh_candles(last)}))
    (out / "^NSEI.json").write_text(json.dumps({"symbol": "^NSEI", "candles": _fh_candles("2026-10-30")}))
    monkeypatch.setattr(fh, "today_ist", lambda: __import__("datetime").date(2026, 11, 2))
    calls = []

    def fake(base, sym, period, retries, sleep, timeout):
        calls.append(sym)
        if sym == "OLDOK":
            return _fh_candles("2026-10-30"), ""
        if sym == "OLDSAME":
            return old, ""
        return None, "network"
    monkeypatch.setattr(fh, "fetch_one_ex", fake)
    monkeypatch.setattr(fh, "fetch_one", lambda *a, **k: fake(*a, **k)[0])
    rc = fh.main([str(out), "--symbols", "OLDOK,OLDFAIL,OLDSAME,FRESH", "--base", "http://x", "--sleep", "0", "--min-bars", "250", "--max-age-days", "5"])
    assert rc == 0 and sorted(calls) == ["OLDFAIL", "OLDOK", "OLDSAME"]            # FRESH is not fetched
    summary = json.loads([l for l in capsys.readouterr().out.splitlines() if l.startswith("{")][-1])
    assert (summary["saved"], summary["refreshed"], summary["stale_kept"], summary["failed"]) == (1, 1, 2, 0)
    assert summary["newest_last_candle"] == "2026-10-30" and summary["oldest_last_candle"] == "2026-10-20"
    assert fh.last_candle_date(json.loads((out / "OLDOK.json").read_text())["candles"]).isoformat() == "2026-10-30"
    assert fh.last_candle_date(json.loads((out / "OLDFAIL.json").read_text())["candles"]).isoformat() == "2026-10-20"     # old file survived


def test_without_max_age_days_a_kept_file_is_never_fetched_again(tmp_path, monkeypatch, capsys):
    import json
    import fetch_history_for_backtest as fh
    out = tmp_path / "h"
    out.mkdir()
    (out / "A.json").write_text(json.dumps({"symbol": "A", "candles": _fh_candles("2026-01-05")}))
    (out / "^NSEI.json").write_text(json.dumps({"symbol": "^NSEI", "candles": _fh_candles("2026-01-05")}))
    monkeypatch.setattr(fh, "fetch_one_ex", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    monkeypatch.setattr(fh, "fetch_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert fh.main([str(out), "--symbols", "A", "--base", "http://x", "--sleep", "0"]) == 0
    summary = json.loads([l for l in capsys.readouterr().out.splitlines() if l.startswith("{")][-1])
    assert summary["kept"] == 1 and summary["saved"] == 0 and "refreshed" not in summary


def test_report_ages_prints_the_freshness_of_a_history_folder_without_the_network(tmp_path, monkeypatch, capsys):
    import json
    import fetch_history_for_backtest as fh
    out = tmp_path / "h"
    out.mkdir()
    for sym, last in (("A", "2026-10-30"), ("B", "2026-10-30"), ("C", "2026-10-21")):
        (out / f"{sym}.json").write_text(json.dumps({"symbol": sym, "candles": _fh_candles(last, 10)}))
    (out / "BAD.json").write_text("not json")
    monkeypatch.setattr(fh, "today_ist", lambda: __import__("datetime").date(2026, 11, 2))
    monkeypatch.setattr(fh, "_get_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    assert fh.main([str(out), "--report-ages", "--max-age-days", "5"]) == 0
    text = capsys.readouterr().out
    assert "3 file(s); newest last candle 2026-10-30, oldest 2026-10-21" in text and "1 older than 5 day(s)" in text and "1 unreadable" in text and "2026-10-30 x2" in text


def test_env_reference_check_names_the_differing_rows(tmp_path):
    repo = _mini_repo(tmp_path)
    subprocess.run([sys.executable, "scripts/gen_env_reference.py"], cwd=repo, check=True, capture_output=True)
    (repo / "services" / "demo-service" / "extra.py").write_text('import os\nos.getenv("STALE_LEFTOVER_VAR", "1")\n')
    r = subprocess.run([sys.executable, "scripts/gen_env_reference.py", "--check"], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 1 and "out of date" in r.stdout and "STALE_LEFTOVER_VAR" in r.stdout


def test_halfsell_run_refuses_before_any_prompt_outside_the_window():
    """E285 round 121: `vm_all.sh halfsell run` outside 09:30-14:30 IST on a weekday says so at once: no admin prompt, no HALFSELL prompt, exit 0."""
    import os
    for now in ("7 1000", "6 1100", "1 0929", "3 1430", "2 2200"):
        r = subprocess.run(["bash", "scripts/vm_all.sh", "halfsell", "run"], cwd=ROOT, input="", capture_output=True, text=True, timeout=30, env={**os.environ, "HALFSELL_NOW": now, "REPO": str(ROOT)})
        assert r.returncode == 0 and "NOT STARTED" in r.stdout and "admin user" not in r.stdout and "Type HALFSELL" not in r.stdout, (now, r.stdout, r.stderr)
    r = subprocess.run(["bash", "scripts/vm_all.sh", "halfsell", "run"], cwd=ROOT, input="", capture_output=True, text=True, timeout=30, env={**os.environ, "HALFSELL_NOW": "2 1000", "REPO": str(ROOT)})
    assert "NOT STARTED" not in r.stdout                                  # inside the window the old flow continues (it asks for the login)
