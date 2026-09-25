"""
tests/test_screening_support.py — offline tests for screening/quality_gate.py and
screening/intraday_eligibility.py (position-stocks-service).

quality_gate decides which price/volume candidates are fundamentally decent enough
to buy; intraday_eligibility remembers which stocks Dhan refuses to trade
intraday so they are never bought again. Both are FAIL-OPEN by design (missing
data / an unreachable table must never block every candidate), so the tests pin
exactly where they reject, where they pass, and that no failure ever raises.

Offline: fake httpx client, in-memory SQLite (plus an optional stand-in for the
sister service's table), pure asyncio.run().

Run from services/position-stocks-service:
    python3 -m pytest tests/test_screening_support.py -q --cov=screening --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import config
import models
from screening import intraday_eligibility as ie
from screening import quality_gate as qg
from screening.quality_gate import QualitySignal


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def pin(monkeypatch):
    for k, v in dict(MIN_FUNDAMENTAL_SCORE=40.0, MIN_TECHNICAL_SCORE=40.0, MIN_MARKET_CAP_CR=500.0,
                     QUALITY_GATE_ENABLED=True, QUALITY_GATE_TIMEOUT_S=2.5, QUALITY_CACHE_MAX_AGE_HOURS=48.0,
                     FUNDAMENTAL_URL="http://fund", TECHNICAL_URL="http://tech", EVENT_URL="http://event").items():
        monkeypatch.setattr(config, k, v)


# ── QualitySignal.passes ─────────────────────────────────────────────────────
class TestPasses:
    def test_all_clear(self):
        ok, why = QualitySignal("X", 70, 60, 5000.0).passes()
        assert ok is True and why == "no red flags"

    @pytest.mark.parametrize("kw,needle", [
        ({"fundamental_score": 39.9}, "fundamental_score"),
        ({"technical_score": 39.9}, "technical_score"),
        ({"market_cap_cr": 499.9}, "market_cap"),
    ])
    def test_present_and_below_floor_rejects(self, kw, needle):
        base = dict(fundamental_score=70, technical_score=70, market_cap_cr=5000.0)
        base.update(kw)
        ok, why = QualitySignal("X", **base).passes()
        assert ok is False and needle in why and "floor" in why

    def test_exactly_at_each_floor_passes(self):
        assert QualitySignal("X", 40.0, 40.0, 500.0).passes()[0] is True

    def test_missing_data_is_never_a_reject(self):
        ok, why = QualitySignal("X").passes()
        assert ok is True and "floor check skipped" in why

    def test_one_missing_field_still_checks_the_others(self):
        assert QualitySignal("X", None, 10.0, None).passes()[0] is False
        assert QualitySignal("X", 10.0, None, None).passes()[0] is False
        assert QualitySignal("X", None, None, 100.0).passes()[0] is False

    def test_fundamentals_are_reported_before_technicals_before_market_cap(self):
        assert "fundamental_score" in QualitySignal("X", 1, 1, 1).passes()[1]
        assert "technical_score" in QualitySignal("X", 90, 1, 1).passes()[1]
        assert "market_cap" in QualitySignal("X", 90, 90, 1).passes()[1]

    def test_positive_signals_are_noted_on_a_pass(self):
        ok, why = QualitySignal("X", 70, 70, 5000.0, has_positive_catalyst=True, bulk_deal_flag=True).passes()
        assert ok and "positive catalyst" in why and "bulk deal" in why

    def test_a_catalyst_never_rescues_a_failing_score(self):
        assert QualitySignal("X", 10, 70, 5000.0, has_positive_catalyst=True).passes()[0] is False

    def test_zero_score_is_present_data_not_missing(self):
        assert QualitySignal("X", 0.0, 70, 5000.0).passes()[0] is False


# ── cache ────────────────────────────────────────────────────────────────────
def cache_row(db, symbol, hours_old, fund=60.0, tech=55.0, mcap=1000.0):
    db.add(models.ScalpQualityCache(symbol=symbol, fundamental_score=fund, technical_score=tech,
                                    market_cap_cr=mcap,
                                    updated_at=datetime.now(timezone.utc) - timedelta(hours=hours_old)))
    db.commit()


class TestCache:
    def test_empty_symbol_list_short_circuits(self, db):
        assert qg.get_cache_batch(db, []) == {}

    def test_returns_fresh_rows_for_requested_symbols_only(self, db):
        cache_row(db, "AAA", 1)
        cache_row(db, "BBB", 1)
        out = qg.get_cache_batch(db, ["AAA", "CCC"])
        assert set(out) == {"AAA"}
        s = out["AAA"]
        assert (s.fundamental_score, s.technical_score, s.market_cap_cr) == (60.0, 55.0, 1000.0)
        assert s.has_positive_catalyst is None

    def test_rows_older_than_the_max_age_are_ignored(self, db):
        cache_row(db, "OLD", 49)
        cache_row(db, "NEW", 47)
        assert set(qg.get_cache_batch(db, ["OLD", "NEW"])) == {"NEW"}

    def test_upsert_creates_a_row(self, db):
        qg.upsert_cache_batch(db, [QualitySignal("AAA", 61.0, 52.0, 900.0)])
        r = db.query(models.ScalpQualityCache).one()
        assert (r.symbol, r.fundamental_score, r.technical_score, r.market_cap_cr) == ("AAA", 61.0, 52.0, 900.0)
        assert r.updated_at is not None

    def test_upsert_never_overwrites_a_cached_value_with_none(self, db):
        cache_row(db, "AAA", 30, fund=60.0, tech=55.0, mcap=1000.0)
        qg.upsert_cache_batch(db, [QualitySignal("AAA", None, 70.0, None)])
        r = db.query(models.ScalpQualityCache).one()
        assert (r.fundamental_score, r.technical_score, r.market_cap_cr) == (60.0, 70.0, 1000.0)

    def test_upsert_refreshes_the_timestamp(self, db):
        cache_row(db, "AAA", 40)
        qg.upsert_cache_batch(db, [QualitySignal("AAA", 61.0)])
        assert len(qg.get_cache_batch(db, ["AAA"])) == 1                # was 40h old, still fresh; now 0h

    def test_upsert_skips_a_symbol_with_nothing_live(self, db):
        cache_row(db, "AAA", 40)
        qg.upsert_cache_batch(db, [QualitySignal("AAA"), QualitySignal("ZZZ")])
        assert db.query(models.ScalpQualityCache).count() == 1

    def test_upsert_handles_a_batch(self, db):
        qg.upsert_cache_batch(db, [QualitySignal("AAA", 61.0), QualitySignal("BBB", None, 50.0)])
        assert db.query(models.ScalpQualityCache).count() == 2

    def test_cache_fallback_fills_only_the_missing_fields(self):
        live = QualitySignal("X", fundamental_score=70.0)
        cached = QualitySignal("X", 10.0, 55.0, 900.0)
        out = qg._apply_cache_fallback(live, cached)
        assert (out.fundamental_score, out.technical_score, out.market_cap_cr) == (70.0, 55.0, 900.0)

    def test_cache_fallback_without_a_cache_is_a_noop(self):
        live = QualitySignal("X", None)
        assert qg._apply_cache_fallback(live, None) is live and live.fundamental_score is None


# ── HTTP fetchers ────────────────────────────────────────────────────────────
class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}

    def json(self):
        return self._body


class FakeClient:
    """Routes by URL prefix. A value that is an Exception instance is raised."""
    def __init__(self, routes):
        self.routes, self.urls = routes, []

    async def get(self, url, timeout=None):
        self.urls.append((url, timeout))
        for prefix, out in self.routes.items():
            if url.startswith(prefix):
                if isinstance(out, BaseException):
                    raise out
                return out
        raise AssertionError(f"unrouted {url}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def run(coro):
    return asyncio.run(coro)


def test_fake_client_raises_on_unrouted_url():
    """Sanity-check FakeClient's own safety net: a URL with no matching
    prefix in `routes` is a test-authoring bug, not a code path under test —
    confirm it actually raises rather than silently misrouting."""
    c = FakeClient({"http://fund": Resp(200, {})})
    with pytest.raises(AssertionError, match="unrouted"):
        run(c.get("http://nowhere/analyze/ABC"))


class TestFetchers:
    def test_fundamental_score_and_market_cap_converted_to_crore(self):
        c = FakeClient({"http://fund": Resp(200, {"fundamental_score": 66.5, "market_cap": 5e10})})
        assert run(qg._fetch_fundamental(c, "ABC")) == (66.5, 5000.0)
        assert c.urls == [("http://fund/analyze/ABC", 2.5)]

    def test_market_cap_falls_back_to_the_raw_block(self):
        c = FakeClient({"http://fund": Resp(200, {"fundamental_score": 50, "market_cap": 0, "raw": {"market_cap": 2e10}})})
        assert run(qg._fetch_fundamental(c, "ABC"))[1] == 2000.0

    def test_unparseable_market_cap_becomes_none_but_score_survives(self):
        c = FakeClient({"http://fund": Resp(200, {"fundamental_score": 50, "market_cap": "n/a"})})
        assert run(qg._fetch_fundamental(c, "ABC")) == (50, None)

    def test_missing_fields_stay_none(self):
        c = FakeClient({"http://fund": Resp(200, {})})
        assert run(qg._fetch_fundamental(c, "ABC")) == (None, None)

    def test_non_200_is_unknown_not_a_reject(self):
        assert run(qg._fetch_fundamental(FakeClient({"http://fund": Resp(503, {"fundamental_score": 1})}), "A")) == (None, None)
        assert run(qg._fetch_technical(FakeClient({"http://tech": Resp(404, {"technical_score": 90})}), "A")) is None
        err = Resp(500, {"has_positive_catalyst": True, "recent_event_score": 9, "bulk_deals": [1]})
        assert run(qg._fetch_event_signal(FakeClient({"http://event": err}), "A")) == (None, None, False)

    def test_network_errors_are_swallowed_and_logged_with_the_class_name(self, caplog):
        with caplog.at_level("INFO"):
            assert run(qg._fetch_fundamental(FakeClient({"http://fund": TimeoutError()}), "SLOW")) == (None, None)
            assert run(qg._fetch_technical(FakeClient({"http://tech": TimeoutError()}), "SLOW")) is None
            assert run(qg._fetch_event_signal(FakeClient({"http://event": TimeoutError()}), "SLOW")) == (None, None, False)
        assert caplog.text.count("TimeoutError") == 3          # empty str(e) must not log a blank reason

    def test_technical_score(self):
        assert run(qg._fetch_technical(FakeClient({"http://tech": Resp(200, {"technical_score": 71})}), "A")) == 71

    def test_event_signal_flags(self):
        ok = Resp(200, {"has_positive_catalyst": True, "recent_event_score": 3.5, "bulk_deals": [{"x": 1}]})
        assert run(qg._fetch_event_signal(FakeClient({"http://event": ok}), "A")) == (True, 3.5, True)
        none = Resp(200, {"has_positive_catalyst": False, "bulk_deals": []})
        assert run(qg._fetch_event_signal(FakeClient({"http://event": none}), "A")) == (False, None, False)

    def test_fund_and_tech_are_fetched_concurrently_not_back_to_back(self):
        order = []

        class Slow(FakeClient):
            async def get(self, url, timeout=None):
                order.append(("start", url))
                await asyncio.sleep(0.01)
                order.append(("end", url))
                return await super().get(url, timeout)
        c = Slow({"http://fund": Resp(200, {"fundamental_score": 1}), "http://tech": Resp(200, {"technical_score": 2})})
        assert run(qg._fetch_fund_tech(c, "A")) == (1, 2, None)
        assert [k for k, _ in order][:2] == ["start", "start"]        # both in flight before either finishes


# ── check() ──────────────────────────────────────────────────────────────────
class TestCheck:
    def patch_client(self, monkeypatch, routes):
        client = FakeClient(routes)
        monkeypatch.setattr(qg.httpx, "AsyncClient", lambda *a, **k: client)
        return client

    def full(self):
        return {"http://fund": Resp(200, {"fundamental_score": 70, "market_cap": 5e10}),
                "http://tech": Resp(200, {"technical_score": 65}),
                "http://event": Resp(200, {"has_positive_catalyst": True, "recent_event_score": 2.0, "bulk_deals": []})}

    def test_all_three_sources_combine(self, monkeypatch):
        self.patch_client(monkeypatch, self.full())
        s = run(qg.check("ABC"))
        assert (s.symbol, s.fundamental_score, s.technical_score, s.market_cap_cr) == ("ABC", 70, 65, 5000.0)
        assert s.has_positive_catalyst is True and s.recent_event_score == 2.0 and s.bulk_deal_flag is False

    def test_disabled_gate_makes_no_network_calls(self, monkeypatch):
        monkeypatch.setattr(config, "QUALITY_GATE_ENABLED", False)
        monkeypatch.setattr(qg.httpx, "AsyncClient", lambda *a, **k: pytest.fail("no HTTP when disabled"))
        s = run(qg.check("ABC"))
        assert s.fundamental_score is None and s.passes()[0] is True

    def test_everything_down_fails_open(self, monkeypatch):
        self.patch_client(monkeypatch, {"http://fund": OSError("refused"), "http://tech": OSError("refused"),
                                        "http://event": OSError("refused")})
        s = run(qg.check("ABC"))
        assert (s.fundamental_score, s.technical_score, s.market_cap_cr) == (None, None, None)
        assert s.passes()[0] is True

    def test_timed_out_fields_come_from_the_cache_live_wins_otherwise(self, monkeypatch):
        routes = self.full()
        routes["http://tech"] = TimeoutError()                       # technical timed out
        self.patch_client(monkeypatch, routes)
        cached = QualitySignal("ABC", fundamental_score=10.0, technical_score=33.0, market_cap_cr=1.0)
        s = run(qg.check("ABC", cached=cached))
        assert s.fundamental_score == 70 and s.market_cap_cr == 5000.0     # live beats cache
        assert s.technical_score == 33.0                                    # cache fills the timeout
        assert s.passes()[0] is False                                       # ...and 33 < 40 now rejects (the 2026-09-17 fix)

    def test_unexpected_error_never_raises_and_still_uses_the_cache(self, monkeypatch):
        self.patch_client(monkeypatch, self.full())

        async def boom(*a, **k):
            raise RuntimeError("gather blew up")
        monkeypatch.setattr(qg, "_fetch_event_signal", boom)
        cached = QualitySignal("ABC", fundamental_score=55.0)
        s = run(qg.check("ABC", cached=cached))
        assert s.fundamental_score == 55.0 and s.technical_score is None


# ── intraday_eligibility ─────────────────────────────────────────────────────
SISTER_DDL = ("CREATE TABLE trade_intraday_restricted (symbol TEXT PRIMARY KEY, first_detected_at TEXT, "
              "last_detected_at TEXT, hit_count INTEGER, last_detail TEXT)")


@pytest.fixture()
def sister(db):
    db.execute(text(SISTER_DDL))
    db.commit()
    return db


def sister_rows(db):
    return db.execute(text("SELECT symbol, hit_count, last_detail FROM trade_intraday_restricted")).fetchall()


class TestRecordRestriction:
    def test_creates_a_normalised_row(self, db):
        ie.record_restriction(db, "  medicaps ", "T2T")
        r = db.query(models.ScalpIntradayRestrictedSecurity).one()
        assert (r.symbol, r.hit_count, r.last_detail) == ("MEDICAPS", 1, "T2T")
        assert r.first_detected_at is not None and r.last_detected_at is not None

    @pytest.mark.parametrize("sym", ["", "   ", None])
    def test_blank_symbol_is_ignored(self, db, sym):
        ie.record_restriction(db, sym, "x")
        assert db.query(models.ScalpIntradayRestrictedSecurity).count() == 0

    def test_second_sighting_increments_and_updates_detail(self, db):
        ie.record_restriction(db, "ABC", "first")
        ie.record_restriction(db, "abc", "second")
        r = db.query(models.ScalpIntradayRestrictedSecurity).one()
        assert r.hit_count == 2 and r.last_detail == "second"

    def test_second_sighting_without_detail_keeps_the_old_detail(self, db):
        ie.record_restriction(db, "ABC", "first")
        ie.record_restriction(db, "ABC")
        assert db.query(models.ScalpIntradayRestrictedSecurity).one().last_detail == "first"

    def test_detail_is_truncated_to_255_chars(self, db):
        ie.record_restriction(db, "ABC", "x" * 400)
        assert len(db.query(models.ScalpIntradayRestrictedSecurity).one().last_detail) == 255

    def test_mirrors_into_the_sister_table_when_present(self, sister):
        ie.record_restriction(sister, "ABC", "rej")
        assert sister_rows(sister) == [("ABC", 1, "rej")]
        ie.record_restriction(sister, "ABC", "rej2")
        assert sister_rows(sister) == [("ABC", 2, "rej2")]

    def test_sister_update_without_detail_keeps_the_old_detail(self, sister):
        ie.record_restriction(sister, "ABC", "rej")
        ie.record_restriction(sister, "ABC")
        assert sister_rows(sister) == [("ABC", 2, "rej")]

    def test_missing_sister_table_never_breaks_recording(self, db):
        ie.record_restriction(db, "ABC", "rej")                       # no trade_intraday_restricted table
        assert db.query(models.ScalpIntradayRestrictedSecurity).count() == 1


class TestQueries:
    def test_bulk_fetch_unions_both_services(self, sister):
        ie.record_restriction(sister, "OWN", "x")
        sister.execute(text("INSERT INTO trade_intraday_restricted VALUES ('THEIRS', 'a', 'b', 1, NULL)"))
        sister.commit()
        assert ie.get_restricted_symbols(sister) == {"OWN", "THEIRS"}

    def test_bulk_fetch_is_empty_when_nothing_is_known(self, db):
        assert ie.get_restricted_symbols(db) == set()

    def test_bulk_fetch_fails_open_on_a_dead_table(self, db, monkeypatch):
        monkeypatch.setattr(db, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        assert ie.get_restricted_symbols(db) == set()

    def test_single_lookup_own_table(self, db):
        ie.record_restriction(db, "ABC")
        assert ie.is_restricted(db, " abc ") is True and ie.is_restricted(db, "XYZ") is False

    def test_single_lookup_sister_table(self, sister):
        sister.execute(text("INSERT INTO trade_intraday_restricted VALUES ('THEIRS', 'a', 'b', 1, NULL)"))
        sister.commit()
        assert ie.is_restricted(sister, "theirs") is True

    @pytest.mark.parametrize("sym", ["", "  ", None])
    def test_blank_symbol_is_not_restricted(self, db, sym):
        assert ie.is_restricted(db, sym) is False

    def test_single_lookup_fails_open_on_a_dead_table(self, db, monkeypatch):
        monkeypatch.setattr(db, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        assert ie.is_restricted(db, "ABC") is False


# ── FakeClient async context-manager protocol (round-30) ─────────────────────
class TestFakeClientContextManager:
    """FakeClient.__aenter__ and __aexit__ are defined but no existing test
    exercises the `async with FakeClient(...) as c:` path — all callers pass
    the client directly as an argument.  This class covers both dunder methods
    so test_screening_support.py line 183 (`return False`) is reached."""

    def test_async_context_manager_enters_and_exits_cleanly(self):
        async def _use():
            routes = {"http://fund": Resp(200, {"fundamental_score": 80, "market_cap": 1e10})}
            async with FakeClient(routes) as c:
                resp = await c.get("http://fund/analyze/TEST")
                assert resp.json()["fundamental_score"] == 80

        run(_use())
