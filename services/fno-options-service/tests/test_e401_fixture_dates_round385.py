"""Round 385: E401 - engine test fixtures dated to the tests' own clock, and scripts/fno_live_check.py showing the live switch overrides."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from helpers import make_intraday  # noqa: E402
from test_engine_paper import NOW  # noqa: E402


def test_default_intraday_bars_end_exactly_at_the_test_clock():
    bars = make_intraday()
    assert bars[-1]["t"] + 300 == int(NOW.timestamp())          # the last bar has just completed at NOW, so nothing is "stale" and nothing is dated after NOW
    assert len(bars) == 40 and bars[1]["t"] - bars[0]["t"] == 300


def test_an_explicit_t0_still_wins():
    assert make_intraday(n=3, t0=1_700_000_000)[0]["t"] == 1_700_000_000


def test_the_engine_sees_fresh_bars_and_enters_instead_of_ignoring_the_intraday_part():
    import test_real_partial as t
    from fno import store as st
    db = st.Store(":memory:")
    b = t.RB()
    t.real(db, b).tick(NOW)
    assert db.open_positions("REAL") and b.sent                   # with the stale bars it scored 29 (FLAT) and made no entry at all


def _run_live_check(tmp_path, monkeypatch, overrides):
    import subprocess
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    (ctl / "overrides.json").write_text(json.dumps({"v": 1, "overrides": overrides}), encoding="utf-8")
    env = {**os.environ, "CONTROL_DIR": str(ctl), "FNO_ENGINE_ENABLED": "false"}     # false = what a fresh `docker compose exec` process sees
    root = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
    # a subprocess on purpose: the script pops `config` from sys.modules, which would change the module the other tests share
    r = subprocess.run([sys.executable, os.path.join(root, "scripts", "fno_live_check.py"), "--db", str(tmp_path / "absent.db")], env=env, capture_output=True, text=True, timeout=60)
    return r.stdout


def test_live_check_shows_the_switch_override_not_just_the_container_env(tmp_path, monkeypatch):
    out = _run_live_check(tmp_path, monkeypatch, {"FNO_ENGINE_ENABLED": {"value": "true"}})
    assert "FNO_ENGINE_ENABLED=true (switch)" in out


def test_live_check_without_an_override_file_falls_back_to_env(tmp_path, monkeypatch):
    out = _run_live_check(tmp_path, monkeypatch, {})
    line = [x for x in out.splitlines() if x.startswith("== switches")][0]
    assert "FNO_ENGINE_ENABLED=false" in line and "(switch)" not in line
