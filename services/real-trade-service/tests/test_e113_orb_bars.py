"""E113: close-based ORB trigger and trigger-bar volume for Real Trade (entry_engine/orb_bars.py), the record fields and the report buckets. Stdlib only
(runs with `python3 tests/test_e113_orb_bars.py` or pytest). Bars come from a fake fetch; the clock is passed in."""
from __future__ import annotations

import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "scripts"))

import config  # noqa: E402
from entry_engine import orb_bars as OB  # noqa: E402
from entry_engine import orb_shadow as OS  # noqa: E402
import trade_records as TR  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 10, 26, tzinfo=IST)           # a Monday


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def bar(h, m, c, v, hi=None):
    t = at(h, m).timestamp()
    return (t, c, hi if hi is not None else c, c, c, v)


RANGE = [bar(9, 15, 100, 1000), bar(9, 16, 100, 2000), bar(9, 17, 100, 1500), bar(9, 18, 100, 1500), bar(9, 19, 100, 1000)]   # average 1400
NOW = at(9, 30, 5)


def test_close_trigger_and_volume_ratio():
    bars = RANGE + [bar(9, 20, 99, 900), bar(9, 21, 103, 2800), bar(9, 22, 105, 9000)]
    f = OB.bar_fields("X", 102.0, NOW, bars)
    assert f["close_trigger"] is True and f["n_range_bars"] == 5
    assert (f["trigger_bar_at"], f["trigger_close"], f["trigger_volume"], f["range_avg_volume"], f["volume_ratio"]) == ("09:21", 103.0, 2800, 1400, 2.0)   # the FIRST close above wins


def test_a_wick_or_a_close_on_the_high_is_not_a_trigger():
    wick = RANGE + [bar(9, 20, 101, 900, hi=110.0), bar(9, 21, 102, 900)]
    f = OB.bar_fields("X", 102.0, NOW, wick)
    assert f["close_trigger"] is False and "trigger_volume" not in f


def test_a_forming_bar_is_never_used():
    bars = RANGE + [bar(9, 30, 120, 5000)]                                   # NOW is 09:30:05 so the 09:30 bar is still forming
    assert OB.bar_fields("X", 102.0, NOW, bars)["close_trigger"] is False


def test_pre_open_and_trigger_time_bars_stay_out_of_the_range_average():
    bars = [bar(9, 10, 100, 999999)] + RANGE + [bar(9, 20, 103, 2800)]
    f = OB.bar_fields("X", 102.0, NOW, bars)
    assert f["n_range_bars"] == 5 and f["range_avg_volume"] == 1400 and f["volume_ratio"] == 2.0


def test_trigger_without_range_bars_has_no_ratio():
    f = OB.bar_fields("X", 102.0, NOW, [bar(9, 21, 103, 2800)])
    assert f["close_trigger"] is True and f["n_range_bars"] == 0 and "volume_ratio" not in f


def test_no_bars_and_junk_rows():
    assert OB.bar_fields("X", 102.0, NOW, []) == {} and OB.bar_fields("X", None, NOW, RANGE) == {}
    f = OB.bar_fields("X", 102.0, NOW, RANGE + [("junk",), None, (1, 2), bar(9, 21, 103, 2800)])
    assert f["close_trigger"] is True and f["volume_ratio"] == 2.0


def test_stale_cache_writes_no_fields_and_is_counted():
    OB._reset_for_tests()
    old = RANGE + [bar(9, 21, 103, 2800)]                                    # newest bar ends 09:22; NOW is 09:30:05 -> 8 minutes behind
    assert OB.fields_for("X", 102.0, NOW, old) == {}
    assert OB.stats()["stale"] == 1
    fresh = RANGE + [bar(9, 28, 103, 2800), bar(9, 29, 103, 100)]           # ends 09:30:00, 5 s behind
    out = OB.fields_for("X", 102.0, NOW, fresh)
    assert out["close_trigger"] is True and OB.stats()["hit"] == 1


def _tick(price, hi=101.0, lo=99.0, vwap=100.0):
    return types.SimpleNamespace(price=price, day_high=hi, day_low=lo, vwap=vwap, spread_pct=0.05)


def _eval_with_bars(bars):
    OS._reset_for_tests(); OB._reset_for_tests()
    OS.evaluate("RBAR", _tick(100.5), at(9, 21))                              # first tick in the capture window fixes range_high 101
    if bars is not None:
        OB._cache[("RBAR", "2026-10-26")] = {"bars": bars, "at": time.monotonic()}
    return OS.evaluate("RBAR", _tick(102.0), at(9, 30, 5))


def test_the_verdict_is_identical_with_and_without_bar_fields():
    saved = OB.FETCH_ON
    OB.FETCH_ON = False
    try:
        with_bars = _eval_with_bars(RANGE + [bar(9, 28, 103, 2800), bar(9, 29, 103, 100)])
        without = _eval_with_bars(None)
        # bars that say "NO close trigger" while the tick says price is above the range high: the verdict must still be the tick's
        no_trig = _eval_with_bars(RANGE + [bar(9, 28, 100, 100), bar(9, 29, 100, 100)])
    finally:
        OB.FETCH_ON = saved
    assert with_bars["close_trigger"] is True and "close_trigger" not in without and no_trig["close_trigger"] is False
    assert without["would_enter"] is True and no_trig["would_enter"] is True and with_bars["would_enter"] is True
    for k in ("would_enter", "reasons", "range_high", "range_low", "limit_price", "stop_price", "stop_pct"):
        assert with_bars[k] == without[k] == no_trig[k], k


def test_orb_off_records_nothing():
    old = config.ORB_MODE
    config.ORB_MODE = "off"
    try:
        OS._reset_for_tests()
        assert OS.evaluate("RBAR", _tick(102.0), at(9, 30)) is None
    finally:
        config.ORB_MODE = old


def test_get_bars_never_waits_for_the_network():
    OB._reset_for_tests()
    saved = OB.FETCH_ON
    OB.FETCH_ON = True
    gate = threading.Event()
    calls = []

    def slow(sym):
        calls.append(sym)
        gate.wait(2.0)
        return [{"date": "2026-10-26 09:15", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1000}]
    OB._fetch = slow
    try:
        t0 = time.monotonic()
        assert OB.get_bars("SLOW", now=at(9, 30)) is None                    # first sight: nothing cached yet
        assert time.monotonic() - t0 < 0.5
        assert OB.get_bars("SLOW", now=at(9, 30)) is None and len(calls) <= 1   # one fetch in flight, not a second one
        gate.set()
        got = None
        for _ in range(100):
            got = OB.get_bars("SLOW", now_mono=time.monotonic(), now=at(9, 30))
            if got:
                break
            time.sleep(0.02)
        assert got and len(got[0]) == 6 and got[0][4] == 100.0               # candle rows became (t, o, h, l, c, v) bars
        assert len(calls) == 1                                               # still inside the refresh window: no new request
    finally:
        OB.FETCH_ON = saved
        OB._reset_for_tests()


def test_a_failed_fetch_is_counted_and_retried_only_after_the_refresh_window():
    OB._reset_for_tests()
    saved = OB.FETCH_ON
    OB.FETCH_ON = True
    n = {"c": 0}

    def boom(sym):
        n["c"] += 1
        raise OSError("market-data down")
    OB._fetch = boom
    try:
        base = time.monotonic()
        assert OB.get_bars("DOWN", now_mono=base, now=at(9, 30)) is None
        for _ in range(100):
            if OB.stats()["failed"]:
                break
            time.sleep(0.02)
        assert OB.stats()["failed"] == 1 and OB.get_bars("DOWN", now_mono=base + 5, now=at(9, 30)) is None and n["c"] == 1
        OB.get_bars("DOWN", now_mono=base + OB.REFRESH_S + 1, now=at(9, 30))
        for _ in range(100):
            if n["c"] == 2:
                break
            time.sleep(0.02)
        assert n["c"] == 2
    finally:
        OB.FETCH_ON = saved
        OB._reset_for_tests()


def test_fetch_switch_off_starts_no_thread():
    OB._reset_for_tests()
    saved = OB.FETCH_ON
    OB.FETCH_ON = False
    OB._fetch = lambda s: (_ for _ in ()).throw(AssertionError("must not fetch"))
    try:
        assert OB.get_bars("OFF", now=at(9, 30)) is None and OB.stats()["requests"] == 0
    finally:
        OB.FETCH_ON = saved
        OB._reset_for_tests()


def test_record_fields_and_buckets():
    f = TR._orb_fields({"would_enter": False, "reasons": ["below_vwap"], "range_pct": 1.0, "close_trigger": True, "volume_ratio": 2.0})
    assert f["orb_close_trigger"] is True and f["orb_volume_ratio"] == 2.0 and f["orb_would_enter"] is False
    assert TR._orb_fields({"would_enter": True})["orb_close_trigger"] is None
    assert TR._orb_fields({"close_trigger": "yes"})["orb_close_trigger"] is None             # only a real boolean counts
    assert TR._orb_fields(None)["orb_volume_ratio"] is None
    assert [TR._orb_trigger_bucket({"orb_close_trigger": v}) for v in (True, False, None)] == ["close_trigger", "no_close_trigger", "no_orb_record"]
    assert [TR._orb_volume_bucket({"orb_volume_ratio": v}) for v in (0.5, 1.0, 1.49, 1.5, 2.49, 2.5, None, "x", True)] == ["<1.0", "1.0-1.5", "1.0-1.5", "1.5-2.5", "1.5-2.5", ">=2.5", "none", "none", "none"]
    assert '"by_orb_close_trigger": _orb_trigger_bucket' in (HERE / "trade_records.py").read_text(encoding="utf-8")


def test_records_report_groups_match_and_never_call_old_trades_no_trigger():
    import records_report as rr
    assert [rr.orb_trigger_bucket(v) for v in (True, False, None, 1, "x")] == ["close_trigger", "no_close_trigger", "no_orb_record", "no_orb_record", "no_orb_record"]
    assert [rr.orb_volume_bucket(v) for v in (0.5, 1.0, 1.5, 2.5, None, True)] == ["<1.0", "1.0-1.5", "1.5-2.5", ">=2.5", "none", "none"]
    assert [rr.orb_verdict_bucket(r) for r in ({}, {"orb_would_enter": True}, {"orb_would_enter": False, "orb_reasons": "below_vwap"}, {"orb_would_enter": "x"})] == \
           ["no_verdict", "orb_would_enter", "orb_would_skip:below_vwap", "no_verdict"]
    rows = [{"position_id": i, "return_pct": r, "net_pnl": r * 10, "gross_pnl": r * 10, "charges": 0, "opened_at": "2026-10-26T04:00:00+00:00", "closed_at": "2026-10-26T09:00:00+00:00",
             "exit_reason": "stop_hit", "orb_close_trigger": t, "orb_volume_ratio": v, "orb_would_enter": w}
            for i, (r, t, v, w) in enumerate([(1.0, True, 2.0, True), (-1.0, False, None, False), (-1.0, None, None, None), (2.0, True, 3.0, True)], 1)]
    g = rr.build_report(rows, min_n=1)["groups"]
    trig = g["opening range: close-based trigger"]
    assert {k: v["n"] for k, v in trig.items()} == {"close_trigger": 2, "no_close_trigger": 1, "no_orb_record": 1}
    assert {k: v["n"] for k, v in g["opening range: trigger-bar volume vs range average"].items()} == {"none": 2, "1.5-2.5": 1, ">=2.5": 1}
    assert g["opening range: would an ORB entry have taken it"]["orb_would_enter"]["n"] == 2


def test_static_rails():
    src = (HERE / "entry_engine" / "orb_bars.py").read_text(encoding="utf-8")
    assert "def get_bars" in src and "threading.Thread(" in src and src.count("_fetch(key[0])") == 1       # the network call is inside the worker only
    assert "would_enter" not in src and "reasons" not in src                                              # it cannot change the verdict
    osrc = (HERE / "entry_engine" / "orb_shadow.py").read_text(encoding="utf-8")
    assert "orb_bars.fields_for(sym, rh, now)" in osrc and osrc.index("orb_bars.fields_for") > osrc.index("limit_price=round(rh")
    assert "httpx" not in osrc
    assert '"bars": orb_bars.stats()' in (HERE / "main.py").read_text(encoding="utf-8")        # the hit / miss / stale counters are visible on the status route


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if fails else 0)
