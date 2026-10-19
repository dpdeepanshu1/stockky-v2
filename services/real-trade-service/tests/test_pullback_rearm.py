"""2026-10-19: pullback re-arm for real-trade-service (entry_engine/pullback_rearm.py). Pure logic, no DB, no network."""
from types import SimpleNamespace as NS

from entry_engine import pullback_rearm as pr


def _fresh():
    pr.reset()


def _tick(price, day_high=None, vwap=None):
    return NS(price=price, day_high=day_high, vwap=vwap)


def test_only_chase_reasons_arm():
    _fresh()
    assert pr.is_chase_reason("Price ₹780 ran +9.6% above signal ₹711 (limit 1.9%). Chasing — skip.")
    assert not pr.is_chase_reason("Price fell -3% below signal. Move may be done")
    assert pr.is_chase_reason("STRUCTURE: good stock, bad entry - chase 2.1 ATR above the nearest demand/imbalance zone (max 1.5)", structure=True)
    assert pr.is_chase_reason("STRUCTURE: good stock, bad entry - price stretched 3.4 ATR above EMA20 (max 3)", structure=True)
    assert not pr.is_chase_reason("STRUCTURE: good stock, bad entry - daily structure is bearish", structure=True)
    assert not pr.is_chase_reason("STRUCTURE: bad entry - purchase zone", structure=True)       # 'purchase' must not match 'chase'
    assert not pr.is_chase_reason(None)


def test_arm_requires_day_gain_and_caps():
    _fresh()
    assert not pr.arm("REAL", "AAA", "x Chasing", 100.0, 0.2)                 # barely up on the day
    assert pr.arm("REAL", "AAA", "x Chasing", 100.0, 3.0, day_high=101.0)
    assert not pr.arm("REAL", "AAA", "x Chasing", 100.0, 3.0)                 # already armed
    assert pr.armed_symbols("REAL") == ["AAA"] and pr.armed_symbols("DEMO") == []
    assert pr.arm("DEMO", "AAA", "x Chasing", 100.0, 3.0)                     # modes are independent


def test_mode_switch_off():
    _fresh()
    import config
    old = getattr(config, "PULLBACK_REARM_MODES", "REAL,DEMO")
    try:
        config.PULLBACK_REARM_MODES = "DEMO"
        assert not pr.arm("REAL", "AAA", "x Chasing", 100.0, 3.0)
        assert pr.arm("DEMO", "AAA", "x Chasing", 100.0, 3.0)
    finally:
        config.PULLBACK_REARM_MODES = old


def test_full_pullback_then_trigger():
    _fresh()
    t0 = 1_000_000.0
    assert pr.arm("REAL", "JUSTDIAL", "Chasing", 780.0, 9.6, day_high=782.0, now=t0)
    sym = "JUSTDIAL"
    # still at the high: wait
    assert pr.scan("REAL", {sym: _tick(780.0, 782.0)}, now=t0 + 10) == []
    # pulls back 1.5% from the 782 peak -> low 770.3, not bouncing yet
    assert pr.scan("REAL", {sym: _tick(770.3, 782.0)}, now=t0 + 60) == []
    # bounce 0.4% but the low is too fresh
    assert pr.scan("REAL", {sym: _tick(773.4, 782.0)}, now=t0 + 70) == []
    # the low has held for 30 s+ and price is still below the peak -> trigger
    hits = pr.scan("REAL", {sym: _tick(773.4, 782.0)}, now=t0 + 120)
    assert len(hits) == 1 and hits[0]["symbol"] == sym and hits[0]["trough"] == 770.3


def test_too_deep_is_dropped_and_new_high_resets():
    _fresh()
    t0 = 1_000_000.0
    pr.arm("REAL", "AAA", "Chasing", 100.0, 3.0, now=t0)
    pr.scan("REAL", {"AAA": _tick(95.0)}, now=t0 + 30)                         # -5%: the move failed
    assert pr.armed_symbols("REAL") == []
    pr.arm("REAL", "BBB", "Chasing", 100.0, 3.0, now=t0)
    pr.scan("REAL", {"BBB": _tick(99.0)}, now=t0 + 10)
    pr.scan("REAL", {"BBB": _tick(103.0)}, now=t0 + 20)                        # new high: old pullback no longer counts
    assert pr.scan("REAL", {"BBB": _tick(103.0)}, now=t0 + 90) == []


def test_below_vwap_waits_and_missing_tick_waits():
    _fresh()
    t0 = 1_000_000.0
    pr.arm("REAL", "AAA", "Chasing", 100.0, 3.0, now=t0)
    pr.scan("REAL", {"AAA": _tick(98.9)}, now=t0 + 5)
    assert pr.scan("REAL", {"AAA": _tick(99.4, vwap=99.9)}, now=t0 + 60) == []   # under VWAP
    assert pr.scan("REAL", {}, now=t0 + 70) == []                               # no tick: just waits
    assert pr.armed_symbols("REAL") == ["AAA"]


def test_expires():
    _fresh()
    t0 = 1_000_000.0
    pr.arm("REAL", "AAA", "Chasing", 100.0, 3.0, now=t0)
    pr.scan("REAL", {"AAA": _tick(100.0)}, now=t0 + 91 * 60)
    assert pr.armed_symbols("REAL") == []


def test_consume_counts_attempts_and_grants_waiver():
    _fresh()
    t0 = 1_000_000.0
    pr.arm("REAL", "AAA", "Chasing", 100.0, 3.0, now=t0)
    assert not pr.has_waiver("REAL", "AAA", now=t0)
    pr.consume("REAL", "AAA", now=t0)
    assert pr.has_waiver("REAL", "AAA", now=t0 + 100)
    assert not pr.has_waiver("DEMO", "AAA", now=t0 + 100)                       # waiver is per mode
    assert not pr.has_waiver("REAL", "AAA", now=t0 + 10_000)                    # expires by itself
    assert pr.arm("REAL", "AAA", "Chasing", 100.0, 3.0, now=t0 + 5)             # attempt 2 of 2 today
    pr.consume("REAL", "AAA", now=t0 + 6)
    assert not pr.arm("REAL", "AAA", "Chasing", 100.0, 3.0, now=t0 + 7)         # daily cap reached


def test_status_shape():
    _fresh()
    pr.arm("REAL", "AAA", "Chasing", 100.0, 3.0)
    s = pr.status()
    assert s["enabled"] and s["armed"][0]["symbol"] == "AAA" and "attempts_today" in s and "waivers" in s
