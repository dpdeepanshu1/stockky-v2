"""tests/test_group268_opening_gate.py - entry_engine/opening_gate.py (group 268).

Entry window opens at 09:15; until OPENING_GATE_SETTLE_IST an automatic entry must pass the opening-quality gate
(minutes since open, change vs previous close, day-range position). A held-back candidate stays queued. Fails closed.
The gate is OFF for the rest of the suite (tests/conftest.py); this file turns it on.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import models
from entry_engine import entry, opening_gate, opening_guard, prev_day
from market_feed.feed import Tick
from risk_engine.engine import RiskResult, RiskVerdict
from tz_utils import IST

from tests.test_entry_evaluate_mode import (  # noqa: F401
    pin, db, make_candidate, quotes, run,
)


_REAL_PREV_DAY = (prev_day.get, prev_day.peek, prev_day.prefetch)    # the autouse fixture below stubs these


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setattr(config, "OPENING_GATE_ENABLED", True)
    monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_ENABLED", True)
    monkeypatch.setattr(config, "OPENING_ENTRY_NOT_BEFORE_IST", "09:15")
    monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_MODES", ("REAL", "DEMO"))
    opening_guard.reset_state()
    # group 270: previous day closed at 0.83 of its 99-102 range (the prev-day check passes unless a test changes `pd`)
    box = {"pd": prev_day.PrevDay(high=102.0, low=99.0, close=101.5, atr=3.0, date="2026-10-08")}
    monkeypatch.setattr(prev_day, "get", lambda s: box["pd"])
    monkeypatch.setattr(prev_day, "peek", lambda s: box["pd"])
    monkeypatch.setattr(prev_day, "prefetch", lambda syms: None)
    yield box
    opening_guard.reset_state()


def at(monkeypatch, hhmm, market_open=True):
    h, m = (int(x) for x in hhmm.split(":"))
    clock = lambda now=None: datetime(2026, 10, 9, h, m, tzinfo=IST)
    for mod in (opening_gate, opening_guard):
        monkeypatch.setattr(mod, "ist_now", clock)
        monkeypatch.setattr(mod, "is_market_open_ist", lambda now=None: market_open)


def tk(price=101.0, prev=100.0, hi=102.0, lo=99.5, symbol="AAA"):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=2.0, source="test",
                day_high=hi, day_low=lo, prev_close=prev)


class TestDefaults:
    def test_guard_default_is_0915_now(self):
        import importlib
        assert config.OPENING_ENTRY_NOT_BEFORE_IST == "09:15" or os.getenv("OPENING_ENTRY_NOT_BEFORE_IST")
        from datetime import time
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(config, "OPENING_ENTRY_NOT_BEFORE_IST", "xx")
            assert opening_guard.not_before() == time(9, 15)
        finally:
            monkey.undo()


class TestActivity:
    def test_active_only_between_open_and_settle(self, monkeypatch):
        at(monkeypatch, "09:14"); assert opening_gate.is_active("REAL") is False
        at(monkeypatch, "09:15"); assert opening_gate.is_active("REAL") is True
        at(monkeypatch, "09:59"); assert opening_gate.is_active("REAL") is True
        at(monkeypatch, "10:00"); assert opening_gate.is_active("REAL") is False

    def test_inactive_when_market_closed_disabled_or_mode_not_covered(self, monkeypatch):
        at(monkeypatch, "09:40", market_open=False); assert opening_gate.is_active("REAL") is False
        at(monkeypatch, "09:40")
        monkeypatch.setattr(config, "OPENING_GATE_ENABLED", False); assert opening_gate.is_active("REAL") is False
        monkeypatch.setattr(config, "OPENING_GATE_ENABLED", True)
        monkeypatch.setattr(config, "OPENING_ENTRY_GUARD_MODES", ("REAL",)); assert opening_gate.is_active("DEMO") is False

    def test_settle_time_configurable_and_bad_value_falls_back(self, monkeypatch):
        monkeypatch.setattr(config, "OPENING_GATE_SETTLE_IST", "10:30")
        at(monkeypatch, "10:15"); assert opening_gate.is_active("REAL") is True
        monkeypatch.setattr(config, "OPENING_GATE_SETTLE_IST", "zz")
        at(monkeypatch, "10:01"); assert opening_gate.is_active("REAL") is False


class TestRejectReason:
    def test_good_tick_passes(self, monkeypatch):
        at(monkeypatch, "09:30")
        assert opening_gate.reject_reason("REAL", tk()) is None

    def test_not_active_never_rejects(self, monkeypatch):
        at(monkeypatch, "10:30")
        assert opening_gate.reject_reason("REAL", tk(price=150.0)) is None
        assert opening_gate.reject_reason("REAL", None) is None

    def test_too_early(self, monkeypatch):
        at(monkeypatch, "09:18")
        assert opening_gate.reject_reason("REAL", tk()).startswith("OPENING_GATE:TOO_EARLY")
        at(monkeypatch, "09:20")
        assert opening_gate.reject_reason("REAL", tk()) is None

    def test_change_vs_prev_close_band(self, monkeypatch):
        at(monkeypatch, "09:30")
        assert opening_gate.reject_reason("REAL", tk(price=99.0, hi=102, lo=98)).startswith("OPENING_GATE:BELOW_PREV_CLOSE")
        assert opening_gate.reject_reason("REAL", tk(price=104.0, hi=110, lo=99)).startswith("OPENING_GATE:EXTENDED")
        assert opening_gate.reject_reason("REAL", tk(price=99.6, hi=102, lo=99)) is None          # -0.4% inside the band

    def test_near_day_high(self, monkeypatch):
        at(monkeypatch, "09:30")
        r = opening_gate.reject_reason("REAL", tk(price=101.9, hi=102.0, lo=99.5))
        assert r.startswith("OPENING_GATE:NEAR_DAY_HIGH")
        monkeypatch.setattr(config, "OPENING_GATE_MAX_RANGE_POS", 0.0)
        assert opening_gate.reject_reason("REAL", tk(price=101.9, hi=102.0, lo=99.5)) is None

    def test_fail_closed_on_missing_data(self, monkeypatch):
        at(monkeypatch, "09:30")
        assert opening_gate.reject_reason("REAL", None) == "OPENING_GATE:NO_PRICE"
        assert opening_gate.reject_reason("REAL", tk(prev=None)) == "OPENING_GATE:NO_PREV_CLOSE"
        assert opening_gate.reject_reason("REAL", tk(hi=None)) == "OPENING_GATE:NO_DAY_RANGE"
        assert opening_gate.reject_reason("REAL", tk(hi=99.0, lo=99.0)) == "OPENING_GATE:NO_DAY_RANGE"
        monkeypatch.setattr(config, "OPENING_GATE_MAX_RANGE_POS", 0.0)
        assert opening_gate.reject_reason("REAL", tk(hi=None, lo=None)) is None   # range not needed when that check is off

    def test_error_holds_the_candidate_back(self, monkeypatch):
        at(monkeypatch, "09:30")
        monkeypatch.setattr(config, "OPENING_GATE_MIN_CHANGE_PCT", "bad")   # forces a TypeError inside the comparison
        assert opening_gate.reject_reason("REAL", tk()) == "OPENING_GATE:ERROR:TypeError"


def _approve_all(monkeypatch):
    monkeypatch.setattr(entry, "risk_evaluate", lambda intent, state: RiskResult(
        verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=intent.qty))


class TestEvaluateModeIntegration:
    def test_held_candidate_stays_queued_and_good_one_is_entered(self, db, monkeypatch):
        make_candidate(db, symbol="GOOD", conviction=80.0)
        make_candidate(db, symbol="CHASE", conviction=80.0)
        quotes(monkeypatch, {"GOOD": tk(symbol="GOOD"),
                             "CHASE": tk(price=101.3, prev=98.0, hi=102.0, lo=98.0, symbol="CHASE")})
        _approve_all(monkeypatch)
        at(monkeypatch, "09:30")
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 1
        left = db.query(models.TradeCandidate).filter_by(consumed=False).all()
        assert [c.symbol for c in left] == ["CHASE"]                                   # queued, not rejected
        assert db.query(models.TradeDecision).filter_by(symbol="CHASE").count() == 0   # nothing logged as a decision

    def test_held_candidate_is_evaluated_normally_after_settle(self, db, monkeypatch):
        make_candidate(db, symbol="CHASE", conviction=80.0)
        quotes(monkeypatch, {"CHASE": tk(price=101.3, prev=98.0, hi=102.0, lo=98.0, symbol="CHASE")})
        _approve_all(monkeypatch)
        at(monkeypatch, "09:30")
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 0
        at(monkeypatch, "10:05")
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1

    def test_gate_off_enters_as_before(self, db, monkeypatch):
        make_candidate(db, symbol="CHASE", conviction=80.0)
        quotes(monkeypatch, {"CHASE": tk(price=101.3, prev=98.0, hi=102.0, lo=98.0, symbol="CHASE")})
        _approve_all(monkeypatch)
        monkeypatch.setattr(config, "OPENING_GATE_ENABLED", False)
        at(monkeypatch, "09:30")
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1

    def test_no_tick_candidate_is_held_back_while_active(self, db, monkeypatch):
        make_candidate(db, symbol="NOQ", conviction=80.0)
        quotes(monkeypatch, {})
        at(monkeypatch, "09:30")
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert db.query(models.TradeCandidate).filter_by(consumed=False).count() == 1


class TestShadowMode:
    def test_shadow_logs_once_keeps_candidate_queued_and_enters_nothing(self, db, monkeypatch, caplog):
        import logging
        opening_gate.reset_shadow()
        monkeypatch.setattr(config, "OPENING_GATE_SHADOW", True)
        make_candidate(db, symbol="GOOD", conviction=80.0)
        quotes(monkeypatch, {"GOOD": tk(symbol="GOOD")})
        _approve_all(monkeypatch)
        at(monkeypatch, "09:30")
        with caplog.at_level(logging.INFO):
            t1 = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
            t2 = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert t1["entered"] == 0 and t2["entered"] == 0
        assert db.query(models.TradeCandidate).filter_by(consumed=False).count() == 1
        assert sum("OPENING_SHADOW would enter GOOD" in r.getMessage() for r in caplog.records) == 1

    def test_shadow_line_carries_stop_and_target_pct(self, db, monkeypatch, caplog):
        import logging
        opening_gate.reset_shadow()
        monkeypatch.setattr(config, "OPENING_GATE_SHADOW", True)
        make_candidate(db, symbol="GOOD", conviction=80.0)
        quotes(monkeypatch, {"GOOD": tk(symbol="GOOD")})            # atr 2.0 on 101 -> 1.98% -> stop 1.5x = 2.97%, target 5.94%
        _approve_all(monkeypatch)
        at(monkeypatch, "09:30")
        with caplog.at_level(logging.INFO):
            run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        msgs = [r.getMessage() for r in caplog.records if "OPENING_SHADOW would enter GOOD" in r.getMessage()]
        assert len(msgs) == 1 and msgs[0].endswith("stop_pct=2.97 target_pct=5.94 pd_close_pos=0.83 atr_pct=2.96")

    def test_after_settle_shadow_stops_and_candidate_is_entered(self, db, monkeypatch):
        opening_gate.reset_shadow()
        monkeypatch.setattr(config, "OPENING_GATE_SHADOW", True)
        make_candidate(db, symbol="GOOD", conviction=80.0)
        quotes(monkeypatch, {"GOOD": tk(symbol="GOOD")})
        _approve_all(monkeypatch)
        at(monkeypatch, "10:05")
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1

    def test_shadow_helpers(self, monkeypatch):
        opening_gate.reset_shadow()
        at(monkeypatch, "09:40")
        monkeypatch.setattr(config, "OPENING_GATE_SHADOW", False)
        assert opening_gate.shadow_active("REAL") is False
        monkeypatch.setattr(config, "OPENING_GATE_SHADOW", True)
        assert opening_gate.shadow_active("REAL") is True
        assert opening_gate.shadow_first_time("REAL", "X") is True
        assert opening_gate.shadow_first_time("REAL", "X") is False
        assert opening_gate.shadow_first_time("DEMO", "X") is True


# ── group 270: previous-day candle checks ────────────────────────────────────
class TestPrevDayChecks:
    def test_weak_previous_close_is_held_back(self, monkeypatch, gate_on):
        at(monkeypatch, "09:30")
        gate_on["pd"] = prev_day.PrevDay(high=102.0, low=99.0, close=99.6, atr=3.0, date="2026-10-08")   # close_pos 0.20
        assert opening_gate.reject_reason("REAL", tk()).startswith("OPENING_GATE:PREV_DAY_WEAK_CLOSE")

    def test_strong_previous_close_passes(self, monkeypatch):
        at(monkeypatch, "09:30")
        assert opening_gate.reject_reason("REAL", tk()) is None

    def test_missing_previous_day_data_fails_closed(self, monkeypatch, gate_on):
        at(monkeypatch, "09:30")
        gate_on["pd"] = None
        assert opening_gate.reject_reason("REAL", tk()) == "OPENING_GATE:NO_PREV_DAY_DATA"

    def test_zero_range_previous_day_skips_the_close_position_check(self, monkeypatch, gate_on):
        at(monkeypatch, "09:30")
        gate_on["pd"] = prev_day.PrevDay(high=100.0, low=100.0, close=100.0, atr=3.0, date="2026-10-08")
        assert opening_gate.reject_reason("REAL", tk()) is None

    def test_close_position_check_can_be_switched_off(self, monkeypatch, gate_on):
        at(monkeypatch, "09:30")
        gate_on["pd"] = None
        monkeypatch.setattr(config, "OPENING_GATE_MIN_PREVDAY_CLOSE_POS", 0.0)
        assert opening_gate.reject_reason("REAL", tk()) is None

    def test_not_active_never_looks_at_previous_day(self, monkeypatch, gate_on):
        at(monkeypatch, "10:30")
        gate_on["pd"] = None
        assert opening_gate.reject_reason("REAL", tk()) is None

    def test_tight_stop_is_held_back(self, monkeypatch):
        at(monkeypatch, "09:30")
        t = tk()                                                    # atr 2.0 on price 101 -> 1.98%; floor 0.3 x 1.98 = 0.59%
        assert opening_gate.reject_reason("REAL", t, stop_pct=0.4).startswith("OPENING_GATE:STOP_TOO_TIGHT")
        assert opening_gate.reject_reason("REAL", t, stop_pct=2.0) is None
        assert opening_gate.reject_reason("REAL", t) is None        # no stop given -> check not applied

    def test_missing_atr_fails_closed_when_a_stop_is_given(self, monkeypatch):
        at(monkeypatch, "09:30")
        t = tk(); t.atr = None
        assert opening_gate.reject_reason("REAL", t, stop_pct=2.0) == "OPENING_GATE:NO_ATR_DATA"
        t.atr = 0
        assert opening_gate.reject_reason("REAL", t, stop_pct=2.0) == "OPENING_GATE:NO_ATR_DATA"

    def test_stop_check_can_be_switched_off(self, monkeypatch):
        at(monkeypatch, "09:30")
        monkeypatch.setattr(config, "OPENING_GATE_MIN_STOP_ATR_FRAC", 0.0)
        t = tk(); t.atr = None
        assert opening_gate.reject_reason("REAL", t, stop_pct=0.1) is None


class TestPrevDayIntegration:
    def test_weak_previous_close_candidate_stays_queued_and_good_one_enters(self, db, monkeypatch, gate_on):
        make_candidate(db, symbol="GOOD", conviction=80.0)
        make_candidate(db, symbol="WEAK", conviction=80.0)
        quotes(monkeypatch, {"GOOD": tk(symbol="GOOD"), "WEAK": tk(symbol="WEAK")})
        _approve_all(monkeypatch)
        good = prev_day.PrevDay(high=102.0, low=99.0, close=101.5, atr=3.0, date="2026-10-08")
        weak = prev_day.PrevDay(high=102.0, low=99.0, close=99.2, atr=3.0, date="2026-10-08")
        monkeypatch.setattr(prev_day, "get", lambda s: weak if s == "WEAK" else good)
        at(monkeypatch, "09:30")
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
        left = db.query(models.TradeCandidate).filter_by(consumed=False).all()
        assert [c.symbol for c in left] == ["WEAK"]
        assert db.query(models.TradeDecision).filter_by(symbol="WEAK").count() == 0

    def test_candidates_are_prefetched_while_the_gate_is_active_only(self, db, monkeypatch):
        seen = []
        monkeypatch.setattr(prev_day, "prefetch", lambda syms: seen.append(sorted(syms)))
        make_candidate(db, symbol="GOOD", conviction=80.0)
        quotes(monkeypatch, {"GOOD": tk(symbol="GOOD")})
        _approve_all(monkeypatch)
        at(monkeypatch, "09:30")
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert seen == [["GOOD"]]
        at(monkeypatch, "10:30")
        make_candidate(db, symbol="LATE", conviction=80.0)
        quotes(monkeypatch, {"GOOD": tk(symbol="GOOD"), "LATE": tk(symbol="LATE")})
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert seen == [["GOOD"]]                                   # nothing prefetched after the settle time

    def test_shadow_mode_also_applies_the_previous_day_check(self, db, monkeypatch, gate_on, caplog):
        import logging
        opening_gate.reset_shadow()
        monkeypatch.setattr(config, "OPENING_GATE_SHADOW", True)
        gate_on["pd"] = prev_day.PrevDay(high=102.0, low=99.0, close=99.2, atr=3.0, date="2026-10-08")
        make_candidate(db, symbol="WEAK", conviction=80.0)
        quotes(monkeypatch, {"WEAK": tk(symbol="WEAK")})
        _approve_all(monkeypatch)
        at(monkeypatch, "09:30")
        with caplog.at_level(logging.INFO):
            run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert not any("OPENING_SHADOW would enter" in r.getMessage() for r in caplog.records)   # rejected, so not a would-enter


# ── prev_day module (same logic as position-stocks-service/screening/prev_day.py) ─────────────────────
def _candles(n=20, last_close=101.0):
    rows = [{"date": f"2026-09-{i + 1:02d} 00:00", "open": 100, "high": 102, "low": 98, "close": 100 + (i % 2)} for i in range(n)]
    rows[-1] = {"date": "2026-10-08 00:00", "open": 100, "high": 103, "low": 99, "close": last_close}
    return rows


class TestPrevDayModule:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.setattr(prev_day, "get", _REAL_PREV_DAY[0])
        monkeypatch.setattr(prev_day, "peek", _REAL_PREV_DAY[1])
        monkeypatch.setattr(prev_day, "prefetch", _REAL_PREV_DAY[2])
        prev_day.reset()
        monkeypatch.setattr(prev_day, "ist_today_str", lambda now=None: "2026-10-09")
        yield
        prev_day.reset()

    def test_parse_candles_prev_day_and_atr(self):
        pdv = prev_day.parse_candles(_candles(), "2026-10-09")
        assert pdv.date == "2026-10-08" and pdv.high == 103 and pdv.low == 99 and pdv.close == 101.0
        assert pdv.atr is not None and 3.5 < pdv.atr < 4.5 and pdv.close_pos == pytest.approx(0.5)
        assert pdv.atr_pct == pytest.approx(pdv.atr / 101.0 * 100.0)

    def test_parse_candles_drops_todays_partial_candle(self):
        rows = _candles() + [{"date": "2026-10-09 09:30", "open": 1, "high": 2, "low": 1, "close": 2}]
        assert prev_day.parse_candles(rows, "2026-10-09").date == "2026-10-08"

    def test_stale_last_candle_is_unusable_when_a_max_age_is_given(self):
        rows = _candles()                                            # last candle 2026-10-08
        assert prev_day.parse_candles(rows, "2026-10-09", 6).date == "2026-10-08"
        assert prev_day.parse_candles(rows, "2026-10-14", 6).date == "2026-10-08"      # 6 days: still ok (long weekend + holiday)
        assert prev_day.parse_candles(rows, "2026-10-15", 6) is None                   # 7 days: stale history
        assert prev_day.parse_candles(rows, "2026-10-15").date == "2026-10-08"          # no limit given -> unchanged behaviour

    def test_worker_applies_the_configured_max_age(self, monkeypatch):
        monkeypatch.setattr(prev_day, "_INLINE", True)
        monkeypatch.setattr(prev_day, "_fetch_candles", lambda sym: _candles())
        monkeypatch.setattr(prev_day, "ist_today_str", lambda now=None: "2026-10-20")   # 12 days after the last candle
        assert prev_day.get("AAA") is None and prev_day.peek("AAA") is None

    def test_parse_candles_short_history_and_junk(self):
        assert prev_day.parse_candles(_candles(5), "2026-10-09").atr is None
        assert prev_day.parse_candles([], "2026-10-09") is None
        assert prev_day.parse_candles(None, "2026-10-09") is None
        assert prev_day.parse_candles([{"date": "2026-10-08", "high": 0, "low": 0, "close": 0}], "2026-10-09") is None
        assert prev_day.parse_candles([{"date": "2026-10-08", "high": 1, "low": 2, "close": 1}], "2026-10-09") is None
        assert prev_day.PrevDay(1, 1, 1, None, "x").atr_pct is None

    def test_get_fetches_once_caches_and_returns_cached(self, monkeypatch):
        calls = []
        monkeypatch.setattr(prev_day, "_INLINE", True)
        monkeypatch.setattr(prev_day, "_fetch_candles", lambda sym: calls.append(sym) or _candles())
        first = prev_day.get("AAA")
        assert first is not None and calls == ["AAA"]
        assert prev_day.get("AAA") is first and calls == ["AAA"]

    def test_real_thread_path_does_not_block_and_fills_the_cache(self, monkeypatch):
        import threading, time
        gate = threading.Event()
        monkeypatch.setattr(prev_day, "_INLINE", False)
        monkeypatch.setattr(prev_day, "_fetch_candles", lambda sym: (gate.wait(5), _candles())[1])
        assert prev_day.get("AAA") is None and "AAA" in prev_day._inflight
        gate.set()
        for _ in range(100):
            if prev_day.peek("AAA") is not None:
                break
            time.sleep(0.02)
        assert prev_day.peek("AAA") is not None and "AAA" not in prev_day._inflight

    def test_failed_fetch_is_not_retried_immediately(self, monkeypatch):
        calls = []
        monkeypatch.setattr(prev_day, "_INLINE", True)

        def boom(sym):
            calls.append(sym)
            raise RuntimeError("history HTTP 503")
        monkeypatch.setattr(prev_day, "_fetch_candles", boom)
        assert prev_day.get("AAA") is None and prev_day.get("AAA") is None
        assert calls == ["AAA"]

    def test_unusable_candles_are_not_cached(self, monkeypatch):
        monkeypatch.setattr(prev_day, "_INLINE", True)
        monkeypatch.setattr(prev_day, "_fetch_candles", lambda sym: [])
        assert prev_day.get("AAA") is None and prev_day.peek("AAA") is None

    def test_inflight_cap(self, monkeypatch):
        monkeypatch.setattr(prev_day, "_INLINE", True)
        for i in range(prev_day.PREVDAY_MAX_INFLIGHT):
            prev_day._inflight.add(f"S{i}")
        monkeypatch.setattr(prev_day, "_fetch_candles", lambda sym: (_ for _ in ()).throw(AssertionError("must not fetch")))
        assert prev_day.get("NEW") is None

    def test_cache_from_an_earlier_day_is_not_used(self):
        prev_day._cache["AAA"] = ("2026-10-08", prev_day.PrevDay(1, 1, 1, None, "2026-10-07"))
        assert prev_day.peek("AAA") is None

    def test_prefetch_starts_fetches_and_never_raises(self, monkeypatch):
        got = []
        monkeypatch.setattr(prev_day, "get", lambda s: got.append(s))
        prev_day.prefetch(["A", "B"]); prev_day.prefetch(None)
        assert got == ["A", "B"]
        monkeypatch.setattr(prev_day, "get", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
        prev_day.prefetch(["A"])                                    # swallowed

    def test_fetch_candles_uses_market_data_history(self, monkeypatch):
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

        class R2(R):
            status_code = 503
        monkeypatch.setattr(C, "get", lambda self, url, params=None: R2())
        with pytest.raises(RuntimeError):
            prev_day._fetch_candles("ABC")
