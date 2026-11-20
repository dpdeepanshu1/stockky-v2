"""round 663 (F3): scripts/fno_rolling_all.sh - dry-run command list, token guard, stage selection. No network, nothing is run."""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, "scripts", "fno_rolling_all.sh")
SIX = ["CORE", "TREND_RIDER", "MOMENTUM_SURGE", "BEAR_SWEEP", "DRIFT_EDGE", "WEEKLY_OPENER"]


def _run(env_extra, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DHAN_")}
    env.update({"FORCE": "1", "ROLL_DIR": str(tmp_path / "r"), "INPUTS": str(tmp_path / "i"), "OUT": str(tmp_path / "o")})
    env.update(env_extra)
    return subprocess.run(["bash", SH], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


def test_syntax():
    assert subprocess.run(["bash", "-n", SH]).returncode == 0


def test_dry_run_lists_12_backtests_and_fetches_both_underlyings(tmp_path):
    r = _run({"DRY_RUN": "1"}, tmp_path)
    assert r.returncode == 0, r.stderr
    bt = [l for l in r.stdout.splitlines() if l.startswith("+ python3 scripts/fno_rolling_backtest.py")]
    assert len(bt) == 12
    for u in ("NIFTY", "SENSEX"):
        for s in SIX:
            assert any(f"--underlying {u} --strategy {s} " in l for l in bt), (u, s)
        assert any(f"fno_rolling_fetch.py --und {u} " in l and "--resume" in l for l in r.stdout.splitlines())
    assert "docker cp" in r.stdout or "not running" in r.stderr


def test_default_strategies_are_the_six_readiness_needs():
    src = open(SH, encoding="utf-8").read()
    assert 'STRATEGIES="${STRATEGIES:-' + " ".join(SIX) + '}"' in src


def test_fetch_without_token_stops_before_doing_anything(tmp_path):
    r = _run({"STAGES": "fetch"}, tmp_path)
    assert r.returncode == 2 and "DHAN_CLIENT_ID" in r.stderr
    assert not (tmp_path / "r").exists()


def test_stage_selection_skips_fetch(tmp_path):
    r = _run({"DRY_RUN": "1", "STAGES": "run", "UNDERLYINGS": "NIFTY"}, tmp_path)
    assert r.returncode == 0
    assert "fno_rolling_fetch.py" not in r.stdout
    assert len([l for l in r.stdout.splitlines() if "fno_rolling_backtest.py" in l and l.startswith("+")]) == 6
