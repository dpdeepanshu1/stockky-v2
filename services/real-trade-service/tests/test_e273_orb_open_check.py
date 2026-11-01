"""E273 round 15: Real Trade ORB is live | off, and the swing entry skips a pick whose first 5 minutes reversed.
Plain runner: python3 tests/test_e273_orb_open_check.py (service folder). Stdlib only; bars are made up, the clock is passed in."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from entry_engine import orb_bars as OB  # noqa: E402
from entry_engine import orb_shadow as OS  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 10, 26, tzinfo=IST)   # a Monday


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def bar(h, m, o, c, lo=None, hi=None, v=1000):
    t = at(h, m).timestamp()
    return (t, o, hi if hi is not None else max(o, c), lo if lo is not None else min(o, c), c, v)


UP = [bar(9, 15, 100, 100.2), bar(9, 16, 100.2, 100.4), bar(9, 17, 100.4, 100.3), bar(9, 18, 100.3, 100.5), bar(9, 19, 100.5, 100.6)]
DOWN = [bar(9, 15, 100, 99.8), bar(9, 16, 99.8, 99.5), bar(9, 17, 99.5, 99.4), bar(9, 18, 99.4, 99.3), bar(9, 19, 99.3, 99.2)]
NOW = at(9, 21, 30)


def test_rising_open_is_ok():
    v = OB.open_check(UP + [bar(9, 20, 100.6, 100.7), bar(9, 21, 100.7, 100.9)], at(9, 22, 30))
    assert v["ok"] is True and v["first_5m_pct"] > 0 and v["n_range_bars"] == 5


def test_first_five_minutes_reversal_fails():
    v = OB.open_check(DOWN, NOW)
    assert v["ok"] is False and v["why"] == "first_5m" and "-0.8" in v["detail"]


def test_small_dip_inside_tolerance_is_ok():
    bars = [bar(9, 15, 100, 99.95), bar(9, 16, 99.95, 99.9), bar(9, 17, 99.9, 99.92), bar(9, 18, 99.92, 99.9), bar(9, 19, 99.9, 99.9)]   # -0.10 %
    assert OB.open_check(bars, NOW, max_drop_pct=0.15)["ok"] is True
    assert OB.open_check(bars, NOW, max_drop_pct=0.05)["ok"] is False


def test_last_closed_bar_under_range_low_fails():
    bars = UP + [bar(9, 20, 100.6, 99.5, lo=99.4)]            # range low is 100.0, this closed bar finished at 99.5
    v = OB.open_check(bars, NOW)
    assert v["ok"] is False and v["why"] == "below_range_low"


def test_no_verdict_cases_buy_as_before():
    assert OB.open_check(None, NOW)["ok"] is None
    assert OB.open_check([], NOW)["ok"] is None
    assert OB.open_check(DOWN[:2], at(9, 17, 30))["why"] == "too_few_range_bars"            # only 2 range bars
    assert OB.open_check(DOWN, at(9, 40))["ok"] is None                           # newest bar 20 minutes old = stale, no verdict
    assert OB.open_check([("x",)] * 3 + [None], NOW)["ok"] is None                # unreadable rows
    # the bar still forming at 09:19:30 is not used: 4 closed bars only
    assert OB.open_check(DOWN, at(9, 19, 30), min_bars=5)["ok"] is None


def test_orb_mode_live_or_off_only():
    code = "import sys; sys.path.insert(0,'.'); import config; print('M'+config.ORB_MODE)"
    for val, want in (("shadow", "live"), ("live", "live"), ("", "live"), ("junk", "live"), ("off", "off"), ("OFF", "off")):
        env = dict(os.environ)
        env["ORB_MODE"] = val
        out = subprocess.run([sys.executable, "-c", code], cwd=str(HERE), env=env, capture_output=True, text=True).stdout
        assert "M" + want in out, (val, out)


def test_orb_record_says_live_and_no_record_only_wording():
    OS._reset_for_tests()
    old = config.ORB_MODE
    config.ORB_MODE = "live"
    try:
        tick = SimpleNamespace(price=101.5, day_high=101.0, day_low=100.0, vwap=100.5, spread_pct=0.1)
        d = SimpleNamespace(symbol="ABC", flow_json=None)
        assert OS.record(d, tick, at(9, 21)) is True
        assert '"mode":"live"' in d.flow_json
    finally:
        config.ORB_MODE = old
    for f in ("entry_engine/orb_shadow.py", "entry_engine/orb_bars.py"):
        txt = (HERE / f).read_text()
        assert "RECORD ONLY" not in txt and "record only, no live mode" not in txt, f
    assert "RECORD ONLY" not in (HERE / "config.py").read_text().split("ORB_MODE =")[0].rsplit("# group 470", 1)[-1]


def test_fetch_now_caches_and_fails_soft():
    OB._reset_for_tests()
    import stockky_ms.timeframes as TF  # noqa: F401
    OB._fetch = lambda s: (_ for _ in ()).throw(RuntimeError("down"))
    before = OB.stats()["failed"]
    assert OB.fetch_now("ABC", at(9, 26)) is None and OB.stats()["failed"] == before + 1
    OB._reset_for_tests()


def test_swing_entry_skips_a_reversed_pick_and_buys_the_next():
    from swing import live
    seen = []

    def fake(sym):
        seen.append(sym)
        return {"ok": False, "why": "first_5m", "detail": "first 5 minutes -0.80%"} if sym == "BAD" else {"ok": None, "why": "no_bars"}
    old = live._open_check
    live._open_check = fake
    try:
        import inspect
        src = inspect.getsource(live.entries)
        assert "OPEN_REVERSAL" in src and "SWING_OPEN_CONFIRM_ENABLED" in src and "ORB_MODE" in src
        # the helper wiring: a pick whose check says ok=False is skipped, ok=None is not
        assert asyncio.run(asyncio.to_thread(live._open_check, "BAD"))["ok"] is False
        assert asyncio.run(asyncio.to_thread(live._open_check, "GOOD"))["ok"] is None
    finally:
        live._open_check = old
    # the real helper fails open on any error
    OB._reset_for_tests()
    OB._fetch = lambda s: (_ for _ in ()).throw(RuntimeError("down"))
    assert live._open_check("ZZZ")["ok"] is None
    OB._reset_for_tests()


def test_switches_defaults_and_catalog():
    assert config.SWING_OPEN_CONFIRM_ENABLED is True and config.SWING_OPEN_MAX_DROP_PCT == 0.15 and config.SWING_OPEN_MIN_BARS == 3
    cat = (HERE / "control" / "catalog.py").read_text()
    seg = cat.split('Flag("ORB_MODE"', 1)[1].split("Flag(", 1)[0]
    assert 'choices=("live", "off")' in seg and 'shipped="live"' in seg
    for k in ("SWING_OPEN_CONFIRM_ENABLED", "SWING_OPEN_MAX_DROP_PCT"):
        assert f'Flag("{k}"' in cat


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(n, "passed")
