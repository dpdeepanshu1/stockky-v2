"""E273 round 9: near the day high, spike chase, VWAP distance and gain room are SIZE PENALTIES (orders/soft_gates.py), not refusals.
penalty = value / its own limit, capped at SOFT_GATE_MAX_PENALTY; only the TOTAL at setup_engine.PENALTY_SKIP skips; SOFT_GATES_MODE=veto = the old walk.
Stdlib only for the pure parts (the entry.py wiring is checked on the source text; the attempt_entry tests live in test_entry.py and need SQLAlchemy)."""
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from orders import entry_timing, gain_room, setup_engine, soft_gates, vwap_gate  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _cfg_value(expr, **env_over):
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("SOFT_GATE")}
    env.update(env_over)
    code = "import sys; sys.path.insert(0,'.'); import config; print('V'+str(" + expr + "))"
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    return next(x for x in r.stdout.splitlines() if x.startswith("V"))[1:]


# ---- mode / helpers ---------------------------------------------------------------------------------------------------
def test_mode_defaults_to_penalty_and_junk_reads_as_penalty():
    assert _cfg_value("config.SOFT_GATES_MODE") == "penalty"
    assert _cfg_value("config.SOFT_GATES_MODE", SOFT_GATES_MODE="log") == "penalty"
    assert _cfg_value("config.SOFT_GATES_MODE", SOFT_GATES_MODE=" VETO ") == "veto"
    assert _cfg_value("config.SOFT_GATE_MAX_PENALTY") == "3.0"
    assert _cfg_value("config.SOFT_GATE_MAX_PENALTY", SOFT_GATE_MAX_PENALTY="9") == "3.9"      # a single gate can never reach PENALTY_SKIP (4.0) alone


def test_ratio_penalty_is_value_over_limit_capped(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    assert soft_gates.ratio_penalty(4.0, 4.0) == 1.0
    assert soft_gates.ratio_penalty(6.0, 4.0) == 1.5
    assert soft_gates.ratio_penalty(40.0, 4.0) == 3.0
    assert soft_gates.ratio_penalty(3.9, 4.0) is None and soft_gates.ratio_penalty(None, 4.0) is None and soft_gates.ratio_penalty(5, 0) is None


def test_range_penalty_is_1_at_the_floor_and_the_cap_at_the_high(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    assert soft_gates.range_penalty(0.91, 0.92) is None
    assert soft_gates.range_penalty(0.92, 0.92) == 1.0
    assert soft_gates.range_penalty(0.96, 0.92) == 2.0
    assert soft_gates.range_penalty(1.0, 0.92) == 3.0


def test_total_skip_only_when_the_sum_reaches_the_engine_limit():
    assert soft_gates.total_skip_reason({"a": 3.0}, setup_engine.PENALTY_SKIP) is None
    r = soft_gates.total_skip_reason({"spike_chase": 3.0, "vwap": 1.5}, setup_engine.PENALTY_SKIP)
    assert r.startswith("SOFT_PENALTIES_TOO_HIGH:total 4.5") and "spike_chase 3.0" in r
    assert soft_gates.total_skip_reason({}, 4.0) is None and soft_gates.total_skip_reason(None, 4.0) is None


def test_mode_helper_reads_the_config_switch(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATES_MODE", "veto", raising=False)
    assert soft_gates.mode() == "veto" and not soft_gates.penalty_mode()
    monkeypatch.setattr(config, "SOFT_GATES_MODE", "whatever", raising=False)
    assert soft_gates.mode() == "penalty" and soft_gates.penalty_mode()


# ---- spike chase ------------------------------------------------------------------------------------------------------
def test_spike_judge_scored_day_spike_penalty_and_unchanged_reason(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    monkeypatch.setattr(config, "SPIKE_DAY_PCT", 4.0, raising=False)
    monkeypatch.setattr(config, "SPIKE_DAY_ATR_MULT", 0.0, raising=False)       # the fixed 4 % day limit
    monkeypatch.setattr(config, "SPIKE_RANGE_POS", 0.85, raising=False)
    why, pen = entry_timing.judge_scored(8.0, 0.95, None)
    assert why.startswith("SPIKE_CHASE:DAY_SPIKE") and pen == 2.0
    assert entry_timing.judge(8.0, 0.95, None) == why                            # the old function is the same decision
    assert entry_timing.judge_scored(30.0, 0.95, None)[1] == 3.0                 # capped
    assert entry_timing.judge_scored(8.0, 0.5, None) == (None, None)             # not at the top of the range
    assert entry_timing.judge_scored(3.0, 0.95, None) == (None, None)


def test_spike_judge_scored_thrust_and_the_larger_penalty_wins(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    monkeypatch.setattr(config, "SPIKE_THRUST_PCT", 1.5, raising=False)
    monkeypatch.setattr(config, "SPIKE_THRUST_M1_MULT", 0.0, raising=False)
    monkeypatch.setattr(config, "SPIKE_NEAR_HIGH_PCT", 0.25, raising=False)
    stats = {"thrust_pct": 3.0, "off_high_pct": 0.1}
    why, pen = entry_timing.judge_scored(None, None, stats)
    assert why.startswith("SPIKE_CHASE:THRUST") and pen == 2.0
    monkeypatch.setattr(config, "SPIKE_DAY_PCT", 4.0, raising=False)
    monkeypatch.setattr(config, "SPIKE_DAY_ATR_MULT", 0.0, raising=False)
    assert entry_timing.judge_scored(6.0, 0.95, {"thrust_pct": 1.8, "off_high_pct": 0.1})[1] == 1.5     # the larger of day 6/4 = 1.5 and thrust 1.8/1.5 = 1.2
    assert entry_timing.judge_scored(None, None, {"thrust_pct": 3.0, "off_high_pct": 0.5}) == (None, None)  # too far under the window high


def test_spike_penalty_off_and_log_modes_give_no_penalty(monkeypatch):
    for m in ("off", "log"):
        monkeypatch.setattr(config, "SPIKE_GUARD_MODE", m, raising=False)
        assert entry_timing.spike_penalty("ABC", 100.0) is None


# ---- VWAP distance ----------------------------------------------------------------------------------------------------
def test_vwap_below_penalty_is_the_gap_over_the_tolerance(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    monkeypatch.setattr(config, "ENTRY_VWAP_TOLERANCE_PCT", 0.15, raising=False)
    monkeypatch.setattr(config, "ENTRY_VWAP_MAX_EXTENSION_PCT", 0.0, raising=False)
    assert vwap_gate.decide_scored(99.4, 99.5) == (None, None)                    # 0.10 % under, inside the tolerance
    why, pen = vwap_gate.decide_scored(99.3, 99.5)                                # 0.201 % under -> 0.201 / 0.15
    assert why.startswith("VWAP_BELOW") and 1.3 < pen < 1.4
    assert vwap_gate.decide_scored(99.0, 99.5)[1] == 3.0                          # 0.5 % under = 3.35x, capped
    assert vwap_gate.decide(99.0, 99.5) == vwap_gate.decide_scored(99.0, 99.5)[0]
    assert vwap_gate.decide_scored(100.0, 0) == (None, None)


def test_vwap_extended_penalty_only_when_the_extension_limit_is_set(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    monkeypatch.setattr(config, "ENTRY_VWAP_TOLERANCE_PCT", 0.15, raising=False)
    monkeypatch.setattr(config, "ENTRY_VWAP_MAX_EXTENSION_PCT", 0.0, raising=False)
    assert vwap_gate.decide_scored(101.0, 100.0) == (None, None)
    monkeypatch.setattr(config, "ENTRY_VWAP_MAX_EXTENSION_PCT", 0.5, raising=False)
    why, pen = vwap_gate.decide_scored(101.0, 100.0)
    assert why.startswith("VWAP_EXTENDED") and pen == 2.0


def test_vwap_penalty_follows_the_switch_and_the_clock(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    monkeypatch.setattr(config, "ENTRY_VWAP_GATE_ENABLED", False, raising=False)
    assert vwap_gate.penalty("ABC", 99.0) is None
    monkeypatch.setattr(config, "ENTRY_VWAP_GATE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ENTRY_VWAP_MIN_TIME_IST", "09:45", raising=False)
    assert vwap_gate.penalty("ABC", 99.0, datetime(2026, 10, 23, 9, 30, tzinfo=IST)) is None     # before 09:45: no opinion
    monkeypatch.setattr(vwap_gate.depth_gate, "_fresh_quote", lambda s: {"avg_price": 99.5}, raising=False)
    monkeypatch.setattr(config, "ENTRY_VWAP_TOLERANCE_PCT", 0.15, raising=False)
    monkeypatch.setattr(config, "ENTRY_VWAP_MAX_EXTENSION_PCT", 0.0, raising=False)
    got = vwap_gate.penalty("ABC", 99.0, datetime(2026, 10, 23, 10, 30, tzinfo=IST))
    assert got and got["why"].startswith("VWAP_BELOW") and got["penalty"] == 3.0


# ---- gain room --------------------------------------------------------------------------------------------------------
def test_gain_room_range_penalty_is_need_over_room(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    monkeypatch.setattr(config, "GAIN_ROOM_MIN_TARGET_FRAC", 0.5, raising=False)
    monkeypatch.setattr(config, "GAIN_ROOM_RANGE_MULT", 2.0, raising=False)
    # low 100, avg range 2 % -> expected high 104; target 2 % needs 1.0 % of room
    assert gain_room.range_penalty(100.0, 101.0, 2.0, 2.0) is None                # 2.97 % left
    p = gain_room.range_penalty(100.0, 103.0, 2.0, 2.0)                           # 0.97 % left
    assert p is not None and 1.0 < p < 1.1
    p2 = gain_room.range_penalty(100.0, 103.5, 2.0, 2.0)                          # 0.48 % left -> 1.0 / 0.48 = ~2.07
    assert p2 is not None and 2.0 < p2 < 2.2
    assert gain_room.range_penalty(100.0, 104.0, 2.0, 2.0) == 3.0                 # no room at all = the cap
    assert gain_room.range_penalty(None, 103.0, 2.0, 2.0) is None


def test_gain_room_odds_penalty_counts_margins_under_the_line(monkeypatch):
    monkeypatch.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    monkeypatch.setattr(config, "GAIN_ROOM_ODDS_MARGIN_PTS", 8.0, raising=False)
    monkeypatch.setattr(config, "GAIN_ROOM_ODDS_MIN_RESOLVED", 25, raising=False)
    from orders import trade_odds
    monkeypatch.setattr(trade_odds, "evaluate", lambda bars, side, s, t: {"p_target": 0.20, "be_win": 0.40, "n_resolved": 30})
    assert gain_room.odds_penalty(["bar"], 1.0, 2.0) == 2.5                       # line 0.32, rate 0.20: 0.12 under = 1.5 margins, + 1.0
    monkeypatch.setattr(trade_odds, "evaluate", lambda bars, side, s, t: {"p_target": 0.35, "be_win": 0.40, "n_resolved": 30})
    assert gain_room.odds_penalty(["bar"], 1.0, 2.0) is None                      # above the refusal line: no penalty
    monkeypatch.setattr(trade_odds, "evaluate", lambda bars, side, s, t: {"p_target": 0.20, "be_win": 0.40, "n_resolved": 5})
    assert gain_room.odds_penalty(["bar"], 1.0, 2.0) is None                      # too few resolved replays: no opinion


def test_gain_room_penalty_off_and_log_modes_give_none(monkeypatch):
    for m in ("off", "log"):
        monkeypatch.setattr(config, "GAIN_ROOM_MODE", m, raising=False)
        assert gain_room.penalty("ABC", 100.0, 1.0, 2.0) is None


# ---- wiring (source text: attempt_entry itself needs SQLAlchemy) -------------------------------------------------------
def test_entry_collects_the_four_penalties_and_skips_only_on_the_total():
    src = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    for key in ('_penalties["near_day_high"]', '_penalties["spike_chase"]', '_penalties["gain_room"]', '_penalties["vwap"]'):
        assert key in src, key
    assert src.count("_penalties: dict = {}") == 1                                  # one dict, declared before the first of them
    assert src.index("_penalties: dict = {}") < src.index('_penalties["near_day_high"]') < src.index('_penalties["below_open"]')
    assert "soft_gates.total_skip_reason(_penalties, setup_engine.PENALTY_SKIP)" in src
    assert "_vwap_reject = None if _soft else vwap_gate.reject_reason" in src      # veto mode keeps the old refusal
    assert "range_reject = _range_gate_reject(" in src and "spike_reject = entry_timing.spike_reason(" in src and "_gr_reject = gain_room.reject_reason(" in src
    # the penalties must all be known BEFORE the setup engine classifies (it reads them) - the vwap one included
    assert src.index('_penalties["vwap"]') < src.index("setup_live.classify_long(")


def test_total_penalty_skip_is_a_per_symbol_skip():
    src = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")                # entry.py needs SQLAlchemy to import: read the prefix tuple as text
    assert '"SOFT_PENALTIES_TOO_HIGH"' in src.split("_SYMBOL_SPECIFIC_SKIP_PREFIXES = (", 1)[1].split("\n)\n", 1)[0]


def test_catalog_has_the_switch():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    blk = cat.split('Flag("SOFT_GATES_MODE"', 1)[1].split("Flag(", 1)[0]
    assert 'choices=("penalty", "veto")' in blk and 'shipped="penalty"' in blk
    blk2 = cat.split('Flag("SOFT_GATE_MAX_PENALTY"', 1)[1].split("Flag(", 1)[0]
    assert 'shipped="3.0"' in blk2
