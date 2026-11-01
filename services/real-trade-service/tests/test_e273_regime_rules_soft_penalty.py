"""E273 round 7: REGIME_RULES_MODE=live turns the regime verdict into a SOFT penalty (a size cut) for a candidate that passes the live floors but misses the
stricter weak-market floors. A candidate already under a live floor is left to the old gates and their exemptions (upper circuit etc.)."""
import config
import pytest

from entry_engine import regime_rules as rr


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for k, v in (("REGIME_RULES_MODE", "live"), ("REGIME_RULES_STRONG_SCORE", 60.0), ("REGIME_RULES_TABLE_JSON", ""), ("ENTRY_MIN_REWARD_RISK", 2.0),
                 ("CANDIDATE_MIN_CONVICTION", 55.0), ("REGIME_RULES_FULL_DEPTH", 0.20), ("REGIME_RULES_SKIP_DEPTH", 0.60)):
        monkeypatch.setattr(config, k, v, raising=False)
    rr.reset_stats()


def _pen(score, conv, r):
    return rr.penalty(rr.evaluate(score, 38, conv, r))


def test_modes():
    # no log / record-only mode exists: log, enforce, blank and junk all read as live
    for raw, want in (("live", "live"), ("log", "live"), ("off", "off"), ("enforce", "live"), ("", "live"), ("junk", "live")):
        config.REGIME_RULES_MODE = raw
        assert rr.mode_for("REAL") == want


def test_nothing_between_the_floors_means_no_penalty():
    assert _pen(30, 70, 3.0) == {"mult": 1.0, "depth": 0.0, "skip": False, "reason": ""}     # weak market, passes the weak floors
    assert _pen(70, 60, 2.2)["mult"] == 1.0                                                    # strong market, passes the live floors
    assert _pen(None, 60, 2.2)["mult"] == 1.0                                                  # no opinion


def test_weak_market_candidate_between_the_floors_is_sized_down():
    # conviction 60 passes the live 55, misses the weak 65 by 7.7% -> 1 - 0.5 * 0.0769/0.20 = 0.808
    p = _pen(30, 60, 3.0)
    assert not p["skip"] and p["mult"] == pytest.approx(0.808, abs=0.002) and "conviction" in p["reason"]
    # R:R 2.0 (right at the live floor) vs 2.5 = 20% under -> the whole cut
    assert _pen(30, 70, 2.0)["mult"] == pytest.approx(0.5, abs=0.001)
    # the worse of the two tests decides, never a product of both
    assert _pen(30, 56, 2.1)["mult"] >= 0.5


def test_a_candidate_under_a_live_floor_is_left_to_the_old_gates():
    # conviction 10 (an upper-circuit name that bypasses the conviction floor) and R:R 1.5 are under the live floors: no penalty, no skip here
    for score in (30, 45, 75):
        p = _pen(score, 10, 3.0)
        assert p["mult"] == 1.0 and not p["skip"]
        p = _pen(score, 70, 1.5)
        assert p["mult"] == 1.0 and not p["skip"]


def test_neutral_and_strong_never_change_the_size():
    for score in (45, 75):
        assert _pen(score, 56, 2.1)["mult"] == 1.0


def test_skip_needs_a_stricter_table():
    config.REGIME_RULES_TABLE_JSON = '{"weak": {"min_rr": 6.0}}'
    p = _pen(30, 70, 2.0)                       # 2.0 vs 6.0 = 67% under, over the live floor
    assert p["skip"] and p["mult"] == 0.0 and p["depth"] >= 0.6


def test_never_above_one_and_bad_config_reads_as_defaults(monkeypatch):
    monkeypatch.setattr(config, "REGIME_RULES_FULL_DEPTH", "junk", raising=False)
    monkeypatch.setattr(config, "REGIME_RULES_SKIP_DEPTH", -3, raising=False)
    p = _pen(30, 60, 3.0)
    assert 0.5 <= p["mult"] <= 1.0 and not p["skip"]
    assert rr.penalty({}) == {"mult": 1.0, "depth": 0.0, "skip": False, "reason": ""}
    assert rr.penalty({"opinion": "ok", "would_reject": True, "risk_scale": "x", "rr": "y", "min_rr": None})["mult"] <= 1.0


def test_table_risk_scale_override_is_respected():
    config.REGIME_RULES_TABLE_JSON = '{"weak": {"risk_scale": 0.25}}'
    assert _pen(30, 70, 2.0)["mult"] == pytest.approx(0.25, abs=0.001)


def test_record_carries_the_penalty():
    v = rr.evaluate(30, 38, 60, 3.0)
    p = rr.penalty(v)
    v["penalty_mult"], v["skipped"] = p["mult"], p["skip"]
    rr.record("infy", "REAL", v, 1500.0)
    row = rr.stats()["recent"][0]
    assert row["penalty_mult"] == pytest.approx(0.808, abs=0.002) and row["skipped"] is False


def test_entry_uses_the_penalty_before_sizing():
    src = open("entry_engine/entry.py").read()
    assert "regime_rules.penalty(_rv)" in src and "adj_risk_pct *= _regime_pen_mult" in src
    # applied after the VIX and calendar multipliers and before the risk cap becomes a quantity
    assert src.index("adj_risk_pct *= _cal_mult") < src.index("adj_risk_pct *= _regime_pen_mult") < src.index("max_trade_risk = account_state.equity")
    assert 'if _regime_mode == "live":' in src


def test_shipped_default_is_live_and_the_catalog_agrees():
    from control import catalog
    fl = [f for f in catalog.FLAGS if f.name == "REGIME_RULES_MODE"] if hasattr(catalog, "FLAGS") else []
    if fl:
        assert fl[0].shipped == "live" and "live" in fl[0].choices
    assert 'or "live"' in open("config.py").read().split("REGIME_RULES_MODE =")[1].split("\n")[0]


def test_a_regime_penalty_holds_the_conviction_upsize():
    from entry_engine import conviction_size as cs
    assert cs.hold_reasons(regime_rules_mult=0.8) == ["regime-rules size-down"]
    assert cs.hold_reasons(regime_rules_mult=1.0) == []
    assert "regime_rules_mult=_regime_pen_mult" in open("entry_engine/entry.py").read()


def test_a_skip_is_a_wait_before_sizing_and_off_computes_nothing():
    src = open("entry_engine/entry.py").read()
    assert "if _regime_skip:" in src and src.index("if _regime_skip:") < src.index("adj_risk_pct   = _conviction_adjusted_risk_pct(")
    assert 'if _regime_mode == "live":' in src and 'mode_for(mode) == "log"' not in src


def test_new_knobs_are_blank_safe_and_defaults_are_the_documented_ones():
    # a fresh interpreter, so reading config.py from the environment cannot disturb the other tests' patched values
    import os
    import subprocess
    import sys

    def read(**env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("REGIME_RULES_")}
        e.update(env)
        out = subprocess.run([sys.executable, "-c", "import config; print(config.REGIME_RULES_MODE, config.REGIME_RULES_FULL_DEPTH, config.REGIME_RULES_SKIP_DEPTH)"],
                             capture_output=True, text=True, env=e, cwd=os.getcwd(), timeout=60)
        assert out.returncode == 0, out.stderr[-400:]
        return out.stdout.split()[-3:]

    assert read(REGIME_RULES_FULL_DEPTH="", REGIME_RULES_SKIP_DEPTH="  ") == ["live", "0.2", "0.6"]
    assert read(REGIME_RULES_MODE="log")[0] == "live"
    assert read(REGIME_RULES_MODE="off")[0] == "off"


def test_stats_count_penalised_and_skipped():
    v = rr.evaluate(30, 38, 60, 3.0)
    p = rr.penalty(v)
    v["penalty_mult"], v["skipped"] = p["mult"], p["skip"]
    rr.record("a", "REAL", v)
    config.REGIME_RULES_TABLE_JSON = '{"weak": {"min_rr": 6.0}}'
    v2 = rr.evaluate(30, 38, 70, 2.0)
    p2 = rr.penalty(v2)
    v2["penalty_mult"], v2["skipped"] = p2["mult"], p2["skip"]
    rr.record("b", "REAL", v2)
    s = rr.stats()
    assert s["penalised"] == 1 and s["skipped"] == 1 and s["recent"][0]["skipped"] is True
