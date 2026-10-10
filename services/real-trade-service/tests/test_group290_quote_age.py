"""group290 (plan phase A, "entries refuse an old price"), real-trade-service side.

1. market_feed.feed: a market-data /quote row carries `age_s` / `as_of` (group 289). get_quote used to stamp every such row
   with the time WE received it, so a minute-old cached price looked brand new. Now the tick's `as_of` is the price's real
   time; a row that says nothing about its age is stamped as before and flagged `age_known=False`.
2. entry_engine.entry: an entry is not built on a price older than ENTRY_MAX_QUOTE_AGE_S (default 10 s). It re-reads the
   symbol once through the priority lane, then holds the candidate queued (up to ENTRY_QUOTE_AGE_HOLD_MIN) or WAITs.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from entry_engine import entry
import market_feed.feed as f
from market_feed.feed import Tick
from risk_engine.engine import RiskResult, RiskVerdict

NOW = datetime(2026, 10, 10, 5, 0, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class TestQuoteRowAsOf:
    def test_age_s_gives_now_minus_age(self):
        assert f._quote_row_as_of({"age_s": 45.5}, now=NOW) == NOW - timedelta(seconds=45.5)

    def test_zero_age_is_now(self):
        assert f._quote_row_as_of({"age_s": 0}, now=NOW) == NOW

    def test_age_s_wins_over_as_of(self):
        q = {"age_s": 10, "as_of": (NOW - timedelta(seconds=999)).isoformat()}
        assert f._quote_row_as_of(q, now=NOW) == NOW - timedelta(seconds=10)

    def test_as_of_used_when_no_age_s(self):
        ts = NOW - timedelta(seconds=30)
        assert f._quote_row_as_of({"as_of": ts.isoformat()}, now=NOW) == ts

    def test_as_of_z_suffix_and_offset_are_read_as_utc(self):
        assert f._quote_row_as_of({"as_of": "2026-10-10T04:59:00Z"}, now=NOW) == NOW - timedelta(seconds=60)
        assert f._quote_row_as_of({"as_of": "2026-10-10T10:29:00+05:30"}, now=NOW) == NOW - timedelta(seconds=60)

    def test_naive_as_of_is_not_guessed(self):
        assert f._quote_row_as_of({"as_of": "2026-10-10T04:59:00"}, now=NOW) is None

    def test_as_of_in_the_future_is_clamped_to_now(self):
        assert f._quote_row_as_of({"as_of": (NOW + timedelta(seconds=40)).isoformat()}, now=NOW) == NOW

    @pytest.mark.parametrize("bad", [-1, -0.001, float("nan"), float("inf"), True, False, "12", None, [], {}, 10 ** 9])
    def test_unusable_age_s_is_ignored(self, bad):
        assert f._quote_row_as_of({"age_s": bad}, now=NOW) is None

    @pytest.mark.parametrize("bad", ["", "   ", "garbage", 12345, None, "2026-13-45T00:00:00+00:00"])
    def test_unusable_as_of_is_ignored(self, bad):
        assert f._quote_row_as_of({"as_of": bad}, now=NOW) is None

    def test_bad_age_s_falls_back_to_a_good_as_of(self):
        ts = NOW - timedelta(seconds=20)
        assert f._quote_row_as_of({"age_s": -5, "as_of": ts.isoformat()}, now=NOW) == ts

    def test_non_dict_and_empty_never_raise(self):
        for q in (None, 5, "x", [], {}):
            assert f._quote_row_as_of(q, now=NOW) is None


class _R:
    def __init__(self, body, status=200):
        self._b, self.status_code, self.text = body, status, "x"

    def json(self):
        return self._b


class _Client:
    def __init__(self, body):
        self.body = body

    async def get(self, url, timeout=None):
        return _R(self.body)


@pytest.fixture
def feed_quiet(monkeypatch):
    monkeypatch.setattr(f, "_note_no_data", lambda s: None)
    monkeypatch.setattr(f, "_note_priced", lambda s: None)
    monkeypatch.setattr(f, "_schedule_atr_refresh", lambda *a, **k: None)
    monkeypatch.setattr(f, "_cached_atr", lambda s: None)
    monkeypatch.setattr(f, "_market_open_now", lambda: False)


def _get(body):
    return asyncio.run(f.get_quote(_Client(body), "ABC", skip_live_quote=True))


def _age(t):
    return (datetime.now(timezone.utc) - t.as_of).total_seconds()


class TestGetQuoteSourceTwoAge:
    def test_cached_price_keeps_its_real_age(self, feed_quiet):
        t = _get({"symbol": "ABC", "price": 50.0, "source": "angelone_rest", "age_s": 62.0})
        assert 61 < _age(t) < 66 and t.age_known is True and t.price == 50.0

    def test_fresh_price_is_fresh(self, feed_quiet):
        t = _get({"symbol": "ABC", "price": 50.0, "source": "dhan", "age_s": 0.4})
        assert _age(t) < 3 and t.age_known is True

    def test_as_of_only_row(self, feed_quiet):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()
        t = _get({"symbol": "ABC", "price": 50.0, "source": "dhan", "as_of": ts})
        assert 24 < _age(t) < 29 and t.age_known is True

    def test_row_without_age_is_stamped_with_receipt_time_and_flagged(self, feed_quiet):
        ts = (datetime.utcnow() - timedelta(seconds=100)).isoformat()      # fetched_at alone is NOT trusted
        t = _get({"symbol": "ABC", "price": 50.0, "source": "yahoo_clean", "fetched_at": ts})
        assert _age(t) < 5 and t.age_known is False

    def test_stale_cooldown_row_still_uses_fetched_at(self, feed_quiet):
        ts = (datetime.utcnow() - timedelta(seconds=100)).isoformat()
        t = _get({"symbol": "ABC", "price": 50.0, "source": "stale_cooldown(angelone_rest)", "fetched_at": ts})
        assert 95 < _age(t) < 110 and t.age_known is True

    def test_age_s_beats_stale_cooldown_fetched_at(self, feed_quiet):
        ts = (datetime.utcnow() - timedelta(seconds=500)).isoformat()
        t = _get({"symbol": "ABC", "price": 50.0, "source": "stale_cooldown(x)", "fetched_at": ts, "age_s": 30})
        assert 29 < _age(t) < 34

    def test_unpriced_row_is_still_no_tick(self, feed_quiet):
        assert _get({"symbol": "ABC", "price": None, "source": "cooldown_unpriced", "age_s": 1.0}) is None


class TestTickAgeKnown:
    def test_default_true(self):
        assert Tick("A", 1.0, NOW, None, "x").age_known is True

    def test_flag_survives_the_stale_last_good_copies(self):
        t = Tick("ABC", 10.0, datetime.now(timezone.utc), None, "src", age_known=False)
        with f._PRIO_LOCK:
            f._PRIO_LAST_GOOD["ABC"] = t
            f._WL_LAST_GOOD["ABC"] = t
        try:
            for fallback in (f._prio_last_good_fallback, f._wl_last_good_fallback):
                got = fallback(["ABC"])
                assert got["ABC"].age_known is False and got["ABC"].source == "stale_last_good(src)"
                assert got["ABC"].as_of == t.as_of                    # the ORIGINAL time, never re-stamped
        finally:
            with f._PRIO_LOCK:
                f._PRIO_LAST_GOOD.pop("ABC", None)
                f._WL_LAST_GOOD.pop("ABC", None)


def _tick(age_s, *, known=True, symbol="TESTCO", price=100.0, atr=2.0):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc) - timedelta(seconds=age_s), atr=atr,
                source="test", age_known=known)


class TestEntryQuoteAgeReason:
    def test_fresh_tick_is_fine(self):
        assert entry._entry_quote_age_reason(_tick(2)) is None

    def test_boundary_is_inclusive(self):
        n = datetime.now(timezone.utc)
        t = Tick("A", 1.0, n - timedelta(seconds=10), None, "x")
        assert entry._entry_quote_age_reason(t, now=n) is None
        assert entry._entry_quote_age_reason(t, now=n + timedelta(seconds=0.5)) is not None

    def test_old_tick_names_age_limit_and_source(self):
        why = entry._entry_quote_age_reason(_tick(35))
        assert why is not None and "35s old" in why and "limit 10s" in why and "test" in why

    def test_env_limit(self, monkeypatch):
        monkeypatch.setenv("ENTRY_MAX_QUOTE_AGE_S", "60")
        assert entry._entry_quote_age_reason(_tick(35)) is None
        assert entry._entry_quote_age_reason(_tick(70)) is not None

    @pytest.mark.parametrize("off", ["0", "-5", "0.0"])
    def test_zero_or_negative_is_off(self, monkeypatch, off):
        monkeypatch.setenv("ENTRY_MAX_QUOTE_AGE_S", off)
        assert entry._entry_quote_age_reason(_tick(9999)) is None

    @pytest.mark.parametrize("bad", ["", "  ", "abc", "nan"])
    def test_blank_or_bad_env_gives_the_default(self, monkeypatch, bad):
        monkeypatch.setenv("ENTRY_MAX_QUOTE_AGE_S", bad)
        assert entry._entry_quote_age_reason(_tick(35)) is not None
        assert entry._entry_quote_age_reason(_tick(2)) is None

    def test_unknown_age_allowed_by_default(self):
        assert entry._entry_quote_age_reason(_tick(500, known=False)) is None

    def test_unknown_age_refused_when_asked(self, monkeypatch):
        monkeypatch.setenv("ENTRY_QUOTE_AGE_UNKNOWN", "Refuse")
        why = entry._entry_quote_age_reason(_tick(0, known=False))
        assert why is not None and "unknown" in why

    def test_unknown_age_refuse_does_not_apply_when_check_is_off(self, monkeypatch):
        monkeypatch.setenv("ENTRY_QUOTE_AGE_UNKNOWN", "refuse")
        monkeypatch.setenv("ENTRY_MAX_QUOTE_AGE_S", "0")
        assert entry._entry_quote_age_reason(_tick(0, known=False)) is None

    def test_future_stamp_counts_as_fresh(self):
        assert entry._entry_quote_age_reason(_tick(-40)) is None

    def test_naive_as_of_is_read_as_utc(self):
        t = Tick("A", 1.0, datetime.utcnow() - timedelta(seconds=50), None, "x")
        assert entry._entry_quote_age_reason(t) is not None

    def test_garbage_never_raises(self):
        class Odd:
            as_of = "yesterday"
            source = None
        assert entry._entry_quote_age_reason(Odd()) is None
        assert entry._entry_quote_age_reason(None) is None


class TestHoldCandidate:
    def _cand(self, minutes_ago):
        class C:
            received_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return C()

    def test_young_candidate_is_held(self):
        assert entry._hold_candidate_for_fresh_price(self._cand(2)) is True

    def test_old_candidate_is_not(self):
        assert entry._hold_candidate_for_fresh_price(self._cand(11)) is False

    def test_zero_disables_holding(self, monkeypatch):
        monkeypatch.setenv("ENTRY_QUOTE_AGE_HOLD_MIN", "0")
        assert entry._hold_candidate_for_fresh_price(self._cand(0)) is False

    def test_naive_received_at_is_utc(self):
        class C:
            received_at = datetime.utcnow() - timedelta(minutes=1)
        assert entry._hold_candidate_for_fresh_price(C()) is True

    def test_missing_or_bad_received_at_is_not_held(self):
        class C:
            received_at = None
        assert entry._hold_candidate_for_fresh_price(C()) is False
        assert entry._hold_candidate_for_fresh_price(object()) is False


@pytest.fixture(autouse=True)
def pin(monkeypatch):
    for k, v in dict(ENTRY_MIN_REWARD_RISK=2.0, ENTRY_COMPOSITE_WEIGHT_CONVICTION=0.65,
                     ENTRY_COMPOSITE_WEIGHT_RR=0.10, ENTRY_COMPOSITE_WEIGHT_DRIFT=0.25,
                     ENTRY_COMPOSITE_RR_CEILING=4.0, MIN_TRADE_VALUE=3000.0,
                     MIN_EDGE_TO_COST_RATIO=3.0, API_GATEWAY_URL="http://gw",
                     COST_MODEL_ENABLED=False, ENTRY_CYCLE_QUALITY_FILTER_ENABLED=False,
                     ENTRY_MAX_NEW_PER_CYCLE=3, ENTRY_MIN_COMPOSITE_SCORE=50.0,
                     ENTRY_VALIDITY_MINUTES=15, ENTRY_ORDER_TYPE="LIMIT",
                     ENTRY_ZONE_UPPER_PCT=0.1, ENTRY_REGIME_OVERRIDE_TOP_N=0).items():
        monkeypatch.setattr(config, k, v)
    for k, v in dict(MIN_REWARD_RISK_RATIO=2.0, MAX_ENTRY_DRIFT_ATR=0.75, CONVICTION_MIDPOINT=65.0,
                     CONVICTION_MAX_SCALE=0.25, REGIME_MIN_SCORE_STATIC=38).items():
        monkeypatch.setattr(entry, k, v)
    for k in ("ENTRY_MAX_QUOTE_AGE_S", "ENTRY_QUOTE_AGE_UNKNOWN", "ENTRY_QUOTE_AGE_HOLD_MIN"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    for m in ("DEMO", "REAL"):
        s.add(models.TradeAccount(mode=m, starting_capital=100000.0, current_equity=100000.0, cash_available=100000.0))
        s.add(models.TradeRiskConfig(mode=m))
    s.commit()
    yield s
    s.close()


def _cand(db, symbol="TESTCO", minutes_ago=0):
    c = models.TradeCandidate(mode="DEMO", symbol=symbol, source_tab="hot_picks", decision_label="BUY NOW",
                              conviction_score=70.0, signal_price=100.0, raw_payload=None, consumed=False,
                              received_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _quotes(monkeypatch, first, second=None):
    """get_quotes answers `first` for the normal call and `second` (default: same) for the priority re-read."""
    calls = []

    async def _q(symbols, priority=False, allow_stale=False):
        calls.append({"symbols": list(symbols), "priority": priority})
        src = second if (priority and second is not None) else first
        return {k: v for k, v in src.items() if k in symbols}
    monkeypatch.setattr(entry, "get_quotes", _q)
    return calls


def _approve(monkeypatch):
    monkeypatch.setattr(entry, "risk_evaluate", lambda intent, acct: RiskResult(
        verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=intent.qty))


class TestEvaluateModeAgeGate:
    def test_fresh_tick_enters_and_makes_no_extra_read(self, db, monkeypatch):
        _cand(db)
        calls = _quotes(monkeypatch, {"TESTCO": _tick(1)})
        _approve(monkeypatch)
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
        assert [c for c in calls if c["priority"]] == []

    def test_old_tick_is_reread_once_and_a_fresh_answer_enters(self, db, monkeypatch):
        _cand(db)
        calls = _quotes(monkeypatch, {"TESTCO": _tick(60)}, {"TESTCO": _tick(1)})
        _approve(monkeypatch)
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
        assert [c for c in calls if c["priority"]] == [{"symbols": ["TESTCO"], "priority": True}]

    def test_still_old_after_reread_holds_the_candidate_queued(self, db, monkeypatch):
        c = _cand(db)
        _quotes(monkeypatch, {"TESTCO": _tick(60)}, {"TESTCO": _tick(40)})
        _approve(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        db.refresh(c)
        assert tally["entered"] == 0 and tally["waited"] == 1
        assert c.consumed is False                                   # judged again next cycle
        assert db.query(models.TradeDecision).count() == 0

    def test_old_candidate_is_consumed_as_a_wait_naming_the_newest_age(self, db, monkeypatch):
        c = _cand(db, minutes_ago=30)
        _quotes(monkeypatch, {"TESTCO": _tick(60)}, {"TESTCO": _tick(35)})
        _approve(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        db.refresh(c)
        why = tally["entry_details"][0]["reasoning"]
        assert tally["entered"] == 0 and c.consumed is True
        assert "Price too old" in why and "35s old" in why and "60s old" not in why
        d = db.query(models.TradeDecision).one()
        assert d.action == "WAIT" and "Price too old" in d.reasoning

    def test_reread_failure_keeps_the_old_tick_and_still_refuses(self, db, monkeypatch):
        _cand(db, minutes_ago=30)
        calls = []

        async def _q(symbols, priority=False, allow_stale=False):
            calls.append(priority)
            if priority:
                raise RuntimeError("market-data down")
            return {"TESTCO": _tick(60)}
        monkeypatch.setattr(entry, "get_quotes", _q)
        _approve(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 0 and "60s old" in tally["entry_details"][0]["reasoning"]
        assert calls == [False, True]

    def test_reread_that_returns_nothing_keeps_the_old_tick(self, db, monkeypatch):
        _cand(db, minutes_ago=30)
        _quotes(monkeypatch, {"TESTCO": _tick(60)}, {})
        _approve(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 0 and "60s old" in tally["entry_details"][0]["reasoning"]

    def test_switch_off_restores_old_behaviour(self, db, monkeypatch):
        monkeypatch.setenv("ENTRY_MAX_QUOTE_AGE_S", "0")
        _cand(db)
        calls = _quotes(monkeypatch, {"TESTCO": _tick(600)})
        _approve(monkeypatch)
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
        assert [c for c in calls if c["priority"]] == []

    def test_unknown_age_enters_by_default(self, db, monkeypatch):
        _cand(db)
        _quotes(monkeypatch, {"TESTCO": _tick(0, known=False)})
        _approve(monkeypatch)
        assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1

    def test_unknown_age_refused_when_asked(self, db, monkeypatch):
        monkeypatch.setenv("ENTRY_QUOTE_AGE_UNKNOWN", "refuse")
        monkeypatch.setenv("ENTRY_QUOTE_AGE_HOLD_MIN", "0")
        _cand(db)
        _quotes(monkeypatch, {"TESTCO": _tick(0, known=False)})
        _approve(monkeypatch)
        tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] == 0 and "unknown" in tally["entry_details"][0]["reasoning"]

    def test_the_price_the_order_uses_is_the_fresh_one(self, db, monkeypatch):
        _cand(db)
        _quotes(monkeypatch, {"TESTCO": _tick(60, price=100.0)}, {"TESTCO": _tick(1, price=100.4)})
        seen = {}

        def _risk(intent, acct):
            seen["entry"] = intent.entry_price
            return RiskResult(verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=intent.qty)
        monkeypatch.setattr(entry, "risk_evaluate", _risk)
        run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert seen["entry"] >= 100.4
