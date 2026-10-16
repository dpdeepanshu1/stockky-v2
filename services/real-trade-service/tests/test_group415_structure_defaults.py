"""group415: structure-rule defaults that were more aggressive than the execution plan allows are back to evidence-gated values.

Gate G2 (benefit) is not met - the A/B verdict is `no_edge` - and G3 (stability) is not started, so until there is evidence:
  * STRUCTURE_STOP_MODE    off   (the ATR stop is used; no structural levels are computed)
  * STRUCTURE_EXIT_MODE    log   (record only; nothing is raised or sold on a structure signal)
  * STRUCTURE_MAX_AGE_DAYS 5.0   (was 7; the plan said 3, but Friday's daily bar is ~3.2-3.4 days old at Monday 09:15 IST, which would make
                                  every Monday entry WAIT while STRUCTURE_REQUIRE_DATA is on)
Each is switched back with one env value. `config` reads the environment at import, so it is loaded here as a private copy.
"""
import importlib.util
import os
from datetime import date, datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VARS = ("STRUCTURE_STOP_MODE", "STRUCTURE_EXIT_MODE", "STRUCTURE_MAX_AGE_DAYS")


def _load(monkeypatch, **env):
    for k in _VARS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("config_group415_copy", os.path.join(HERE, "config.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_defaults_are_the_evidence_gated_ones(monkeypatch):
    c = _load(monkeypatch)
    assert c.STRUCTURE_STOP_MODE == "off"
    assert c.STRUCTURE_EXIT_MODE == "log"
    assert c.STRUCTURE_MAX_AGE_DAYS == 5.0


@pytest.mark.parametrize("v,expect", [("on", "on"), ("ENFORCE", "on"), ("off", "off"), ("shadow", "off"), ("", "off"), ("junk", "off")])
def test_stop_mode_is_opt_in(monkeypatch, v, expect):
    assert _load(monkeypatch, STRUCTURE_STOP_MODE=v).STRUCTURE_STOP_MODE == expect


@pytest.mark.parametrize("v,expect", [("exit", "exit"), ("Tighten", "tighten"), ("off", "off"), ("log", "log"), ("junk", "log"), ("", "log")])
def test_exit_mode_values(monkeypatch, v, expect):
    assert _load(monkeypatch, STRUCTURE_EXIT_MODE=v).STRUCTURE_EXIT_MODE == expect


def test_max_age_can_be_restored(monkeypatch):
    assert _load(monkeypatch, STRUCTURE_MAX_AGE_DAYS="7").STRUCTURE_MAX_AGE_DAYS == 7.0


def test_stop_mode_off_means_no_structural_levels_and_never_use(monkeypatch):
    """With the default, structural_levels returns early: no stop / target, use False (the ATR stop is what the entry uses)."""
    import config
    from entry_engine import structure_gate as sg
    summary = {"ms_stop": 95.0, "ms_target": 110.0, "ms_rr_flags": []}
    monkeypatch.setattr(config, "STRUCTURE_STOP_MODE", "off", raising=False)
    out = sg.structural_levels(summary, 100.0, 96.0, 108.0)
    assert out["use"] is False and out["stop"] is None and out["target"] is None
    monkeypatch.setattr(config, "STRUCTURE_STOP_MODE", "on", raising=False)
    monkeypatch.setattr(config, "STRUCTURE_STOP_MAX_RISK_MULT", 1.6, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_STOP_MIN_RISK_MULT", 0.5, raising=False)
    out = sg.structural_levels(summary, 100.0, 96.0, 108.0)
    assert out["use"] is True and out["stop"] == 95.0


def test_five_days_clears_every_2026_trading_day():
    """Age of the last daily bar at the 15:30 IST close of each remaining 2026 trading day, worst case (bar stamped IST midnight).
    The plan's 3 days fails every Monday; 4.5 would fail the afternoon of Mon 28 Dec (Fri 25 Dec is a holiday); 5 clears all of them.
    NSE 2027 holidays are not published yet (expected December 2026) - re-check then."""
    ist = timezone(timedelta(hours=5, minutes=30))
    hol = {date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25)}

    def trading(d):
        return d.weekday() < 5 and d not in hol
    worst, d = 0.0, date(2026, 10, 19)
    while d <= date(2026, 12, 31):
        if trading(d):
            p = d - timedelta(days=1)
            while not trading(p):
                p -= timedelta(days=1)
            age = (datetime(d.year, d.month, d.day, 15, 30, tzinfo=ist) - datetime(p.year, p.month, p.day, tzinfo=ist)).total_seconds() / 86400.0
            worst = max(worst, age)
        d += timedelta(days=1)
    assert 4.5 < worst < 5.0


def test_env_example_documents_the_new_defaults():
    env = open(os.path.join(HERE, "..", "..", ".env.example"), encoding="utf-8").read()
    assert "# STRUCTURE_STOP_MODE=off" in env and "# STRUCTURE_EXIT_MODE=log" in env
    assert "# STRUCTURE_MAX_AGE_DAYS=5 " in env and "# STRUCTURE_PREPARE_UPGRADE=0" in env
