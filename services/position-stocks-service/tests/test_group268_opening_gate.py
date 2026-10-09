"""group 268: opening-quality gate (screening/opening_gate.py) - entry window opens at 09:15, gated until the settle time."""
from datetime import datetime

import pytest

import config
from feed import ws_client
from screening import opening_gate as og
from screening import prev_day, trade_gates
from tz_utils import IST

DAY = (2026, 10, 9)   # a Friday, normal NSE trading day


def at(h, m, s=0):
    return datetime(*DAY, h, m, s, tzinfo=IST)


def ts(h, m, s=0):
    return at(h, m, s).timestamp()


class World:
    """Fake feed: day stats (open, high, low, prev_close), one tick buffer, Nifty readings."""

    def __init__(self, mp):
        self.mp = mp
        self.stats = (100.5, 103.0, 100.0, 100.0)
        # opening range 09:15-09:30: 30 ticks between 100.0 and 102.0
        self.ticks = [(ts(9, 15) + i * 30, 100.0 + (i % 5) * 0.5) for i in range(30)]
        self.nifty, self.nifty_prev = 0.2, 0.3
        mp.setattr(ws_client, "get_day_stats", lambda s: self.stats)
        mp.setattr(ws_client, "get_tick_buffer", lambda s: self.ticks)
        mp.setattr(trade_gates, "last_nifty_change_pct", lambda: self.nifty)
        mp.setattr(trade_gates, "last_nifty_prev_close_pct", lambda: self.nifty_prev)
        mp.setattr(og, "is_market_open_ist", lambda now=None: True)
        # group 270: previous day closed at 0.83 of its 99-102 range; daily ATR 3.0 (2.96% of the close)
        self.pd = prev_day.PrevDay(high=102.0, low=99.0, close=101.5, atr=3.0, date="2026-10-08")
        mp.setattr(prev_day, "get", lambda s: self.pd)
        mp.setattr(prev_day, "peek", lambda s: self.pd)


@pytest.fixture()
def w(monkeypatch):
    monkeypatch.setattr(config, "OPENING_GATE_ENABLED", True)
    monkeypatch.setattr(config, "MARKET_GATE_ENABLED", True)
    return World(monkeypatch)


def test_inactive_at_and_after_settle_time(w):
    assert og.is_active(at(9, 59)) is True
    assert og.is_active(at(10, 0)) is False
    assert og.reject_reason("AAA", 50.0, at(10, 30)) is None      # even a bad price passes after settle: normal rules apply


def test_inactive_before_open_or_when_market_closed(w, monkeypatch):
    assert og.is_active(at(9, 14)) is False
    monkeypatch.setattr(og, "is_market_open_ist", lambda now=None: False)
    assert og.is_active(at(9, 40)) is False


def test_disabled_switch(w, monkeypatch):
    monkeypatch.setattr(config, "OPENING_GATE_ENABLED", False)
    assert og.reject_reason("AAA", 1.0, at(9, 40)) is None


def test_settle_time_is_configurable(w, monkeypatch):
    monkeypatch.setattr(config, "OPENING_GATE_SETTLE_IST", "10:30")
    assert og.is_active(at(10, 15)) is True
    monkeypatch.setattr(config, "OPENING_GATE_SETTLE_IST", "garbage")
    assert og.is_active(at(10, 1)) is False                        # bad value falls back to 10:00


def test_good_candidate_passes_after_the_opening_range(w):
    assert og.reject_reason("AAA", 101.9, at(9, 40)) is None


def test_too_early(w):
    r = og.reject_reason("AAA", 101.0, at(9, 18))
    assert r.startswith("OPENING_GATE:TOO_EARLY")


@pytest.mark.parametrize("open_px,code", [(104.0, "GAP_UP"), (98.5, "GAP_DOWN")])
def test_gap_limits(w, open_px, code):
    w.stats = (open_px, 105.0, 98.0, 100.0)
    r = og.reject_reason("AAA", 102.0, at(9, 40))
    assert r.startswith(f"OPENING_GATE:{code}")


def test_gap_down_within_limit_is_allowed_through_to_next_check(w):
    w.stats = (99.5, 103.0, 99.0, 100.0)            # -0.5% gap
    r = og.reject_reason("AAA", 101.9, at(9, 40))
    assert r is None or not r.startswith("OPENING_GATE:GAP")


def test_below_open_and_below_prev_close(w):
    w.stats = (101.0, 103.0, 100.0, 100.0)
    assert og.reject_reason("AAA", 100.9, at(9, 40)).startswith("OPENING_GATE:BELOW_OPEN")
    w.stats = (99.8, 103.0, 99.0, 100.0)
    assert og.reject_reason("AAA", 99.9, at(9, 40)).startswith("OPENING_GATE:BELOW_PREV_CLOSE")


def test_near_day_high(w):
    w.stats = (100.5, 103.0, 100.0, 100.0)
    r = og.reject_reason("AAA", 102.9, at(9, 40))          # range_pos 0.97
    assert r.startswith("OPENING_GATE:NEAR_DAY_HIGH")


def test_range_position_just_under_the_cap_passes_that_check(w):
    w.stats = (100.5, 102.0, 100.0, 100.0)
    assert og.reject_reason("AAA", 101.7, at(9, 40)) is None   # range_pos 0.85 < 0.88; inside the opening range


def test_opening_range_lower_half_rejected_once_complete(w):
    w.stats = (100.5, 103.0, 100.0, 100.0)
    r = og.reject_reason("AAA", 100.6, at(9, 40))          # or_pos 0.30 (OR low 100.0, high 102.0)
    assert r.startswith("OPENING_GATE:BELOW_OR_MID")


def test_extended_far_above_opening_range_rejected(w):
    w.stats = (100.5, 108.0, 100.0, 100.0)
    r = og.reject_reason("AAA", 103.5, at(9, 40))          # 1.47% above OR high 102.0
    assert r.startswith("OPENING_GATE:EXTENDED_ABOVE_OR")


def test_or_position_not_checked_while_the_range_is_still_forming(w):
    w.stats = (100.5, 103.0, 100.0, 100.0)
    w.ticks = [(ts(9, 15) + i * 20, 100.0 + (i % 3) * 0.5) for i in range(30)]   # ticks up to ~09:25
    assert og.reject_reason("AAA", 100.6, at(9, 25)) is None


def test_missing_data_fails_closed(w):
    w.stats = None
    assert og.reject_reason("AAA", 101.0, at(9, 40)) == "OPENING_GATE:NO_DAY_STATS"
    w.stats = (None, 103.0, 100.0, 100.0)
    assert og.reject_reason("AAA", 101.0, at(9, 40)) == "OPENING_GATE:NO_OPEN_OR_PREV_CLOSE"
    w.stats = (100.5, 103.0, 100.0, 100.0)
    w.ticks = w.ticks[:3]
    assert og.reject_reason("AAA", 101.9, at(9, 40)) == "OPENING_GATE:NO_OPENING_RANGE_DATA"
    w.ticks = []
    assert og.reject_reason("AAA", 101.9, at(9, 40)) == "OPENING_GATE:NO_OPENING_RANGE_DATA"
    assert og.reject_reason("AAA", 0.0, at(9, 40)) == "OPENING_GATE:NO_PRICE"


def test_ticks_outside_the_opening_window_do_not_count(w):
    w.ticks = [(ts(9, 40) + i, 101.0) for i in range(30)]    # nothing between 09:15 and 09:30
    assert og.reject_reason("AAA", 101.5, at(9, 45)) == "OPENING_GATE:NO_OPENING_RANGE_DATA"


def test_nifty_checks(w):
    w.nifty = -0.05
    assert og.reject_reason("AAA", 101.9, at(9, 40)).startswith("OPENING_GATE:NIFTY_BELOW_OPEN")
    w.nifty, w.nifty_prev = 0.1, -0.3
    assert og.reject_reason("AAA", 101.9, at(9, 40)).startswith("OPENING_GATE:NIFTY_BELOW_PREV_CLOSE")
    w.nifty = None
    assert og.reject_reason("AAA", 101.9, at(9, 40)) == "OPENING_GATE:NO_NIFTY_DATA"


def test_nifty_check_skipped_when_market_gate_disabled(w, monkeypatch):
    monkeypatch.setattr(config, "MARKET_GATE_ENABLED", False)
    w.nifty = w.nifty_prev = None
    assert og.reject_reason("AAA", 101.9, at(9, 40)) is None


@pytest.mark.parametrize("name", ["OPENING_GATE_MAX_GAP_UP_PCT", "OPENING_GATE_MAX_GAP_DOWN_PCT", "OPENING_GATE_MAX_RANGE_POS"])
def test_zero_disables_a_single_check(w, monkeypatch, name):
    monkeypatch.setattr(config, name, 0.0)
    w.stats = (104.0, 110.0, 98.0, 100.0) if name.endswith("GAP_UP_PCT") else (98.5, 103.0, 98.0, 100.0) if name.endswith("DOWN_PCT") else (100.5, 103.0, 100.0, 100.0)
    price = 104.5 if name.endswith("GAP_UP_PCT") else 98.6 if name.endswith("DOWN_PCT") else 102.95
    r = og.reject_reason("AAA", price, at(9, 40))
    assert r is None or not r.startswith(("OPENING_GATE:GAP", "OPENING_GATE:NEAR_DAY_HIGH"))


def test_internal_error_fails_closed(w, monkeypatch):
    monkeypatch.setattr(ws_client, "get_day_stats", lambda s: (_ for _ in ()).throw(RuntimeError("feed down")))
    assert og.reject_reason("AAA", 101.9, at(9, 40)) == "OPENING_GATE:ERROR:RuntimeError"


def test_entry_features_text(w):
    t = og.entry_features("AAA", 101.5, at(9, 40))
    assert "gap=+0.50%" in t and "vs_open=+1.00%" in t and "range_pos=0.50" in t and "or_pos=0.75" in t and "nifty_prev=+0.30" in t


def test_entry_features_never_raises_and_can_be_empty(w, monkeypatch):
    monkeypatch.setattr(ws_client, "get_day_stats", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    assert og.entry_features("AAA", 101.5, at(9, 40)) == ""


def test_entry_default_window_now_opens_at_0915():
    import main
    assert (main._NO_ENTRY_BEFORE.hour, main._NO_ENTRY_BEFORE.minute) == (9, 15)


# ── group 269: shadow mode ───────────────────────────────────────────────────
def test_shadow_active_only_with_flag_and_inside_window(w, monkeypatch):
    monkeypatch.setattr(config, "OPENING_GATE_SHADOW", False)
    assert og.shadow_active(at(9, 40)) is False
    monkeypatch.setattr(config, "OPENING_GATE_SHADOW", True)
    assert og.shadow_active(at(9, 40)) is True
    assert og.shadow_active(at(10, 5)) is False
    monkeypatch.setattr(config, "OPENING_GATE_ENABLED", False)
    assert og.shadow_active(at(9, 40)) is False


def test_shadow_first_time_once_per_symbol_per_day(w):
    og.reset_shadow()
    assert og.shadow_first_time("AAA", at(9, 40)) is True
    assert og.shadow_first_time("AAA", at(9, 41)) is False
    assert og.shadow_first_time("BBB", at(9, 41)) is True
    assert og.shadow_first_time("AAA", datetime(2026, 10, 12, 9, 40, tzinfo=IST)) is True


# ── group 270: previous-day candle checks ────────────────────────────────────
def test_prev_day_weak_close_rejected(w):
    w.pd = prev_day.PrevDay(high=102.0, low=99.0, close=99.6, atr=3.0, date="2026-10-08")     # close_pos 0.20
    assert og.reject_reason("AAA", 101.9, at(9, 40)).startswith("OPENING_GATE:PREV_DAY_WEAK_CLOSE")


def test_prev_day_strong_close_passes(w):
    assert og.reject_reason("AAA", 101.9, at(9, 40)) is None


def test_prev_day_data_missing_fails_closed(w):
    w.pd = None
    assert og.reject_reason("AAA", 101.9, at(9, 40)) == "OPENING_GATE:NO_PREV_DAY_DATA"


def test_prev_day_checks_can_be_switched_off(w, monkeypatch):
    w.pd = None
    monkeypatch.setattr(config, "OPENING_GATE_MIN_PREVDAY_CLOSE_POS", 0.0)
    monkeypatch.setattr(config, "OPENING_GATE_MIN_STOP_ATR_FRAC", 0.0)
    assert og.reject_reason("AAA", 101.9, at(9, 40)) is None


def test_close_pos_check_off_but_stop_check_on_still_needs_data(w, monkeypatch):
    w.pd = None
    monkeypatch.setattr(config, "OPENING_GATE_MIN_PREVDAY_CLOSE_POS", 0.0)
    assert og.reject_reason("AAA", 101.9, at(9, 40)) == "OPENING_GATE:NO_PREV_DAY_DATA"
    w.pd = prev_day.PrevDay(high=102.0, low=99.0, close=99.6, atr=3.0, date="2026-10-08")      # weak close ignored when the check is off
    assert og.reject_reason("AAA", 101.9, at(9, 40)) is None


def test_zero_range_prev_day_skips_only_the_close_position_check(w):
    w.pd = prev_day.PrevDay(high=100.0, low=100.0, close=100.0, atr=3.0, date="2026-10-08")
    assert w.pd.close_pos is None
    assert og.reject_reason("AAA", 101.9, at(9, 40)) is None


def test_stop_reject(w):
    assert og.stop_reject("AAA", 0.5, at(9, 40)).startswith("OPENING_GATE:STOP_TOO_TIGHT")   # floor 0.3 x 2.96% = 0.89%
    assert og.stop_reject("AAA", 0.9, at(9, 40)) is None
    assert og.stop_reject("AAA", 0.5, at(10, 30)) is None                                      # gate not active
    w.pd = None
    assert og.stop_reject("AAA", 2.0, at(9, 40)) == "OPENING_GATE:NO_ATR_DATA"
    w.pd = prev_day.PrevDay(high=102.0, low=99.0, close=101.5, atr=None, date="2026-10-08")
    assert og.stop_reject("AAA", 2.0, at(9, 40)) == "OPENING_GATE:NO_ATR_DATA"


def test_stop_reject_switch_and_error(w, monkeypatch):
    monkeypatch.setattr(config, "OPENING_GATE_MIN_STOP_ATR_FRAC", 0.0)
    assert og.stop_reject("AAA", 0.1, at(9, 40)) is None
    monkeypatch.setattr(config, "OPENING_GATE_MIN_STOP_ATR_FRAC", 0.3)
    monkeypatch.setattr(prev_day, "get", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    assert og.stop_reject("AAA", 2.0, at(9, 40)) == "OPENING_GATE:ERROR:RuntimeError"


def test_entry_features_include_prev_day(w):
    t = og.entry_features("AAA", 101.5, at(9, 40))
    assert "pd_close_pos=0.83" in t and "atr_pct=2.96" in t


def _candles(n=20, last_close=101.0):
    rows = [{"date": f"2026-09-{i + 1:02d} 00:00", "open": 100, "high": 102, "low": 98, "close": 100 + (i % 2)} for i in range(n)]
    rows[-1] = {"date": "2026-10-08 00:00", "open": 100, "high": 103, "low": 99, "close": last_close}
    return rows


def test_parse_candles_prev_day_and_atr():
    pdv = prev_day.parse_candles(_candles(), "2026-10-09")
    assert pdv.date == "2026-10-08" and pdv.high == 103 and pdv.low == 99 and pdv.close == 101.0
    assert pdv.atr is not None and 3.5 < pdv.atr < 4.5 and pdv.close_pos == pytest.approx(0.5)


def test_parse_candles_drops_todays_partial_candle():
    rows = _candles() + [{"date": "2026-10-09 09:30", "open": 1, "high": 2, "low": 1, "close": 2}]
    assert prev_day.parse_candles(rows, "2026-10-09").date == "2026-10-08"


def test_stale_last_candle_is_unusable_when_a_max_age_is_given():
    rows = _candles()                                                # last candle 2026-10-08
    assert prev_day.parse_candles(rows, "2026-10-14", 6).date == "2026-10-08"
    assert prev_day.parse_candles(rows, "2026-10-15", 6) is None     # 7 days old: stale history
    assert prev_day.parse_candles(rows, "2026-10-15").date == "2026-10-08"   # no limit given -> unchanged


def test_worker_applies_the_configured_max_age(monkeypatch):
    prev_day.reset()
    monkeypatch.setattr(prev_day, "_INLINE", True)
    monkeypatch.setattr(prev_day, "_fetch_candles", lambda sym: _candles())
    monkeypatch.setattr(prev_day, "ist_today_str", lambda now=None: "2026-10-20")      # 12 days after the last candle
    assert prev_day.get("AAA") is None and prev_day.peek("AAA") is None
    prev_day.reset()


def test_parse_candles_short_history_has_no_atr_and_junk_is_none():
    assert prev_day.parse_candles(_candles(5), "2026-10-09").atr is None
    assert prev_day.parse_candles([], "2026-10-09") is None
    assert prev_day.parse_candles([{"date": "2026-10-08", "high": 0, "low": 0, "close": 0}], "2026-10-09") is None
    assert prev_day.parse_candles(None, "2026-10-09") is None
    assert prev_day.parse_candles([{"date": "2026-10-08", "high": 1, "low": 2, "close": 1}], "2026-10-09") is None   # high < low


def test_get_fetches_in_background_once_caches_and_backs_off(monkeypatch):
    prev_day.reset()
    calls = []
    monkeypatch.setattr(prev_day, "_INLINE", True)
    monkeypatch.setattr(prev_day, "_fetch_candles", lambda sym: calls.append(sym) or _candles())
    monkeypatch.setattr(prev_day, "ist_today_str", lambda now=None: "2026-10-09")
    first = prev_day.get("AAA")
    assert first is not None and calls == ["AAA"]
    assert prev_day.get("AAA") is first and calls == ["AAA"]            # cached, no second fetch
    prev_day.reset()


def test_get_real_thread_path_never_blocks_and_fills_the_cache(monkeypatch):
    prev_day.reset()
    import threading
    gate = threading.Event()
    monkeypatch.setattr(prev_day, "_INLINE", False)
    monkeypatch.setattr(prev_day, "ist_today_str", lambda now=None: "2026-10-09")

    def slow(sym):
        gate.wait(5)
        return _candles()
    monkeypatch.setattr(prev_day, "_fetch_candles", slow)
    assert prev_day.get("AAA") is None                                   # miss returns at once; the fetch is still blocked
    assert "AAA" in prev_day._inflight
    gate.set()
    for _ in range(100):
        if prev_day.peek("AAA") is not None:
            break
        import time as _t
        _t.sleep(0.02)
    assert prev_day.peek("AAA") is not None and "AAA" not in prev_day._inflight
    prev_day.reset()


def test_failed_fetch_returns_none_and_is_not_retried_immediately(monkeypatch):
    prev_day.reset()
    calls = []
    monkeypatch.setattr(prev_day, "_INLINE", True)
    monkeypatch.setattr(prev_day, "ist_today_str", lambda now=None: "2026-10-09")

    def boom(sym):
        calls.append(sym)
        raise RuntimeError("history HTTP 503")
    monkeypatch.setattr(prev_day, "_fetch_candles", boom)
    assert prev_day.get("AAA") is None and prev_day.get("AAA") is None
    assert calls == ["AAA"]
    prev_day.reset()


def test_unusable_candles_are_not_cached(monkeypatch):
    prev_day.reset()
    monkeypatch.setattr(prev_day, "_INLINE", True)
    monkeypatch.setattr(prev_day, "ist_today_str", lambda now=None: "2026-10-09")
    monkeypatch.setattr(prev_day, "_fetch_candles", lambda sym: [])
    assert prev_day.get("AAA") is None and prev_day.peek("AAA") is None
    prev_day.reset()


def test_inflight_cap(monkeypatch):
    prev_day.reset()
    monkeypatch.setattr(prev_day, "_INLINE", True)
    for i in range(prev_day.PREVDAY_MAX_INFLIGHT):
        prev_day._inflight.add(f"S{i}")
    monkeypatch.setattr(prev_day, "_fetch_candles", lambda sym: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert prev_day.get("NEW") is None
    prev_day.reset()


def test_cache_from_an_earlier_day_is_not_used(monkeypatch):
    prev_day.reset()
    prev_day._cache["AAA"] = ("2026-10-08", prev_day.PrevDay(1, 1, 1, None, "2026-10-07"))
    monkeypatch.setattr(prev_day, "ist_today_str", lambda now=None: "2026-10-09")
    assert prev_day.peek("AAA") is None
    prev_day.reset()


def test_fetch_candles_uses_market_data_history(monkeypatch):
    import httpx
    seen = {}

    class R:
        status_code = 200
        def json(self): return {"candles": [{"date": "x"}]}

    class C:
        def __init__(self, **k): seen["timeout"] = k.get("timeout")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None): seen["url"], seen["params"] = url, params; return R()
    monkeypatch.setattr(httpx, "Client", C)
    assert prev_day._fetch_candles("ABC.NS") == [{"date": "x"}]
    assert seen["url"].endswith("/history/ABC") and seen["params"] == {"period": "1mo", "interval": "1d"}
    class R2(R): status_code = 503
    monkeypatch.setattr(C, "get", lambda self, url, params=None: R2())
    with pytest.raises(RuntimeError):
        prev_day._fetch_candles("ABC")
