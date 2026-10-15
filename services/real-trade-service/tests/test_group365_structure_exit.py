"""group365: exit_engine/structure_exit.py - daily structure while a position is open. Pure tests (no DB, no network)."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config  # noqa: E402
from exit_engine import structure_exit as sx  # noqa: E402

# structure close 2026-10-09 (Fri); position opened Tue 2026-10-06 -> bars after entry day: Wed, Thu, Fri = 3
ENTRY = 100.0
OPENED = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)          # 09:30 IST on the 6th


def _sum(**kw):
    base = {"ms_quality": "ok", "ms_ver": "1.0.0", "ms_asof": date.today().isoformat(), "ms_price": 110.0, "ms_atr": 4.0,
            "ms_choch_dir": None, "ms_choch_bars": None, "ms_bos_dir": None, "ms_bos_bars": None, "ms_bos_failed": None,
            "ms_stop": 104.0, "ms_trend": "bullish"}
    base.update(kw)
    return base


def _ev(summary, ltp=110.0, stop=96.0, mode="tighten", opened=None, atr=4.0, entry=ENTRY):
    return sx.evaluate(summary, entry_price=entry, ltp=ltp, current_stop=stop, atr=atr,
                       opened_at=opened if opened is not None else datetime(2026, 1, 2, tzinfo=timezone.utc), exit_mode=mode)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_EXIT_MODE", "tighten", raising=False)
    monkeypatch.setattr(config, "STRUCTURE_EXIT_TIGHT_ATR", 0.75, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_EXIT_CHOCH_BARS", 3, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_EXIT_BOS_BARS", 3, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_TRAIL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_TRAIL_MIN_ATR", 1.0, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_MAX_AGE_DAYS", 3650.0, raising=False)      # fixtures use real dates; age is not under test here
    monkeypatch.setattr(config, "STRUCTURE_MAX_PRICE_DEV_PCT", 15.0, raising=False)
    sx.reset()
    yield
    sx.reset()


# ---- dates ----
def test_sessions_after_counts_weekdays_only():
    assert sx.sessions_after(date(2026, 10, 6), date(2026, 10, 9)) == 3
    assert sx.sessions_after(date(2026, 10, 9), date(2026, 10, 12)) == 1          # Fri -> Mon: Sat/Sun skipped
    assert sx.sessions_after(date(2026, 10, 9), date(2026, 10, 9)) == 0
    assert sx.sessions_after(date(2026, 10, 9), date(2026, 10, 8)) == 0
    assert sx.sessions_after(None, date(2026, 10, 8)) == 0 and sx.sessions_after(date(2026, 10, 8), None) == 0


def test_to_date_accepts_strings_datetimes_and_epochs():
    assert sx._to_date("2026-10-09") == date(2026, 10, 9)
    assert sx._to_date(datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)) == date(2026, 10, 10)     # 01:30 IST next day
    assert sx._to_date(1_760_000_000) == sx._to_date(1_760_000_000_000)
    assert sx._to_date("garbage") is None and sx._to_date(None) is None


# ---- signals ----
def test_signal_before_entry_is_not_news():
    s = _sum(ms_asof="2026-10-09", ms_choch_dir="bearish", ms_choch_bars=3)
    sigs = sx.bearish_signals(s, sx.sessions_after(date(2026, 10, 6), date(2026, 10, 9)))
    assert sigs == []


def test_signal_after_entry_counts_and_window_is_enforced():
    s = _sum(ms_choch_dir="bearish", ms_choch_bars=1)
    assert [x["kind"] for x in sx.bearish_signals(s, 3)] == ["bearish_choch"]
    assert sx.bearish_signals(_sum(ms_choch_dir="bearish", ms_choch_bars=4), 9) == []
    assert sx.bearish_signals(_sum(ms_choch_dir="bullish", ms_choch_bars=0), 9) == []


def test_failed_bearish_bos_is_ignored_and_fresh_one_counts():
    assert sx.bearish_signals(_sum(ms_bos_dir="bearish", ms_bos_bars=1, ms_bos_failed=True), 5) == []
    assert [x["kind"] for x in sx.bearish_signals(_sum(ms_bos_dir="bearish", ms_bos_bars=1, ms_bos_failed=False), 5)] == ["bearish_bos"]


def test_no_closed_bar_since_entry_means_no_signal():
    assert sx.bearish_signals(_sum(ms_choch_dir="bearish", ms_choch_bars=0), 0) == []


# ---- tighten ----
def test_bearish_signal_in_profit_tightens_to_ltp_minus_atr_fraction():
    v = _ev(_sum(ms_asof="2026-10-09", ms_choch_dir="bearish", ms_choch_bars=0), ltp=110.0, stop=96.0, opened=OPENED)
    assert v["opinion"] == "ok" and v["action"] == "tighten" and v["in_profit"] is True
    assert v["new_stop"] == 107.0
    assert v["signals"][0]["kind"] == "bearish_choch"


def test_tighten_never_goes_below_breakeven_and_never_lowers_a_higher_stop():
    v = _ev(_sum(ms_asof="2026-10-09", ms_bos_dir="bearish", ms_bos_bars=0, ms_bos_failed=False), ltp=101.0, stop=96.0, opened=OPENED)
    assert v["action"] == "tighten" and v["new_stop"] == 100.0
    v2 = _ev(_sum(ms_asof="2026-10-09", ms_choch_dir="bearish", ms_choch_bars=0), ltp=110.0, stop=109.0, opened=OPENED)
    assert v2["action"] == "none" and v2["new_stop"] is None


def test_losing_position_is_never_touched():
    v = _ev(_sum(ms_asof="2026-10-09", ms_choch_dir="bearish", ms_choch_bars=0), ltp=98.0, stop=94.0, opened=OPENED)
    assert v["action"] == "none" and v["in_profit"] is False and v["new_stop"] is None and v["signals"]


def test_new_stop_is_strictly_below_price():
    v = _ev(_sum(ms_asof="2026-10-09", ms_choch_dir="bearish", ms_choch_bars=0), ltp=100.02, stop=90.0, opened=OPENED, atr=0.001)
    assert v["new_stop"] is not None and v["new_stop"] < 100.02


# ---- modes ----
def test_off_and_log_modes_change_nothing():
    s = _sum(ms_asof="2026-10-09", ms_choch_dir="bearish", ms_choch_bars=0)
    assert _ev(s, opened=OPENED, mode="off")["opinion"] == "off"
    v = _ev(s, opened=OPENED, mode="log")
    assert v["opinion"] == "ok" and v["signals"] and v["action"] == "none" and v["new_stop"] is None


def test_exit_mode_sells_only_in_profit_on_a_signal():
    s = _sum(ms_asof="2026-10-09", ms_choch_dir="bearish", ms_choch_bars=0)
    assert _ev(s, ltp=110.0, opened=OPENED, mode="exit")["action"] == "exit"
    assert _ev(s, ltp=97.0, stop=94.0, opened=OPENED, mode="exit")["action"] == "none"
    assert _ev(_sum(ms_asof="2026-10-09"), ltp=110.0, opened=OPENED, mode="exit")["action"] in ("none", "trail")


def test_unknown_mode_falls_back_to_the_configured_one(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_EXIT_MODE", "bogus", raising=False)
    assert sx.mode() == "exit"          # group 399: the fallback default is exit
    monkeypatch.setattr(config, "STRUCTURE_EXIT_MODE", "exit", raising=False)
    assert sx.mode() == "exit"


# ---- structure trail ----
def test_trail_follows_the_higher_low_when_clear_of_price_and_above_entry():
    v = _ev(_sum(ms_stop=104.0), ltp=110.0, stop=96.0)
    assert v["action"] == "trail" and v["new_stop"] == 104.0 and v["signals"] == []


def test_trail_needs_level_at_or_above_entry_clear_of_price_and_above_current_stop():
    assert _ev(_sum(ms_stop=98.0), ltp=110.0, stop=96.0)["action"] == "none"
    assert _ev(_sum(ms_stop=108.0), ltp=110.0, stop=96.0)["action"] == "none"
    assert _ev(_sum(ms_stop=104.0), ltp=110.0, stop=105.0)["action"] == "none"
    assert _ev(_sum(ms_stop=104.0), ltp=99.0, stop=90.0)["action"] == "none"


def test_trail_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_TRAIL_ENABLED", False, raising=False)
    assert _ev(_sum(ms_stop=104.0), ltp=110.0, stop=96.0)["action"] == "none"


# ---- no opinion ----
@pytest.mark.parametrize("summary", [None, {}, _sum(ms_quality="insufficient"), _sum(ms_quality="stale"), _sum(ms_atr=None)])
def test_missing_or_unusable_structure_is_no_opinion(summary):
    v = _ev(summary, opened=OPENED)
    assert v["opinion"] == "no_opinion" and v["action"] == "none" and v["new_stop"] is None


def test_split_like_price_gap_is_no_opinion():
    v = _ev(_sum(ms_price=110.0, ms_choch_dir="bearish", ms_choch_bars=0), ltp=50.0, stop=40.0, opened=OPENED)
    assert v["opinion"] == "no_opinion" and v["action"] == "none"


def test_garbage_input_never_raises():
    for bad in ("x", 5, [], {"ms_quality": "ok"}, {"ms_quality": "ok", "ms_atr": "nan", "ms_price": "abc"}):
        v = sx.evaluate(bad, entry_price="a", ltp=None, current_stop="z", atr="q", opened_at=object(), exit_mode="tighten")
        assert v["action"] == "none"


# ---- counters ----
def test_record_counts_and_ring():
    s = _sum(ms_asof="2026-10-09", ms_choch_dir="bearish", ms_choch_bars=0)
    v = _ev(s, ltp=110.0, opened=OPENED)
    sx.record("TCS", "REAL", v, applied=True, ltp=110.0)
    sx.record("INFY", "DEMO", _ev(None), applied=False)
    sx.record("WIPRO", "DEMO", _ev(s, opened=OPENED, mode="log"), applied=False, ltp=110.0)
    st = sx.stats()
    c = st["counters"]
    assert c["evaluated"] == 3 and c["no_opinion"] == 1 and c["tightened"] == 1 and c["bearish"] == 2 and c["log_only"] == 1
    assert st["recent"][0]["symbol"] == "WIPRO" and st["recent"][1]["symbol"] == "TCS" and st["recent"][1]["applied"] is True
    sx.reset()
    assert sx.stats()["counters"]["evaluated"] == 0 and sx.stats()["recent"] == []


def test_exit_loop_wiring_is_present_and_before_breakeven():
    src = open(os.path.join(os.path.dirname(HERE), "exit_engine", "exit.py"), encoding="utf-8").read()
    i, j = src.index("3b. Daily market structure"), src.index("4. Breakeven stop")
    assert i < j and "structure_exit" in src[i:j] and "STRUCTURE_EXIT_MODE" in src[i:j]
    assert "STRUCTURE_TIGHTEN" in src[i:j] and "STRUCTURE_TRAIL" in src[i:j] and '"structure_exit"' in src[i:j]


def test_status_route_returns_exit_block():
    src = open(os.path.join(os.path.dirname(HERE), "main.py"), encoding="utf-8").read()
    assert '"exit": _exit_stats' in src and "structure_exit" in src


# ---- cache-only read for the exit loop ----
def test_peek_reads_only_the_fresh_cache_and_never_does_io():
    import time
    from entry_engine import structure_gate as sg
    sg.reset()
    assert sg.peek_summary("TCS") is None
    sg._cache["TCS"] = (time.monotonic() + 60, {"ms_quality": "ok"})
    assert sg.peek_summary("tcs") == {"ms_quality": "ok"}
    sg._cache["TCS"] = (time.monotonic() - 1, {"ms_quality": "ok"})
    assert sg.peek_summary("TCS") is None
    sg._cache["INFY"] = (time.monotonic() + 60, None)
    assert sg.peek_summary("INFY") is None
    sg.reset()


def test_warm_starts_one_background_fetch_per_symbol(monkeypatch):
    import asyncio
    from entry_engine import structure_gate as sg
    sg.reset()
    sg._inflight.clear()
    calls = []

    async def fake_fetch(sym):
        calls.append(sym)
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(sg, "fetch_summary", fake_fetch)
    monkeypatch.setattr(config, "STRUCTURE_FETCH_ENABLED", True, raising=False)

    async def go():
        a = sg.warm("TCS")
        b = sg.warm("TCS")
        await asyncio.sleep(0.01)
        return a, b

    a, b = asyncio.run(go())
    assert a is True and b is False and calls == ["TCS"] and not sg._inflight
    assert sg.warm("TCS") is False                                              # no running loop -> quietly False


def test_warm_is_off_when_fetching_is_disabled(monkeypatch):
    import asyncio
    from entry_engine import structure_gate as sg
    sg.reset()
    monkeypatch.setattr(config, "STRUCTURE_FETCH_ENABLED", False, raising=False)

    async def go():
        return sg.warm("TCS")

    assert asyncio.run(go()) is False


def test_exit_loop_uses_the_cache_not_a_network_call():
    src = open(os.path.join(os.path.dirname(HERE), "exit_engine", "exit.py"), encoding="utf-8").read()
    i, j = src.index("3b. Daily market structure"), src.index("4. Breakeven stop")
    assert "peek_summary" in src[i:j] and "warm(" in src[i:j] and "get_summary" not in src[i:j]
