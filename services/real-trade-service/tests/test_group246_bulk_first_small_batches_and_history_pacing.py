"""group246 (Priority 1 of the 2026-10-08 log review): held positions timed out because the 3-minute candidate /
entry cycle sent hundreds of market-data calls. Pinned here:
  * volume-shock / standard-track analyses use the quote the bulk prefetch returned (no GET /quote per symbol);
  * candidate /history calls are paced by an in-flight cap;
  * small non-priority batches (entry candidates) are priced bulk-first from FEED_SMALL_BATCH_BULK_MIN_SYMBOLS.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import intraday_eligibility
import models
import candidate_engine.candidates as cd
from market_feed import feed as f

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


def _run(coro):
    return asyncio.run(coro)


# ── bulk prefetch returns the quotes ────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _PostClient:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    async def post(self, url, json=None, timeout=None):
        self.calls.append(json["symbols"])
        if self.exc:
            raise self.exc
        return self.resp


def test_prefetch_returns_usable_rows_keyed_by_clean_upper_symbol():
    body = {"quotes": [
        {"symbol": "tcs.ns", "price": 100.0, "previous_close": 98.0},
        {"symbol": "INFY", "cmp": 50.0},
        {"symbol": "ZEROCO", "price": 0},
        {"symbol": "BADPX", "price": "x"},
        {"symbol": "", "price": 5},
        "junk",
    ]}
    client = _PostClient(_Resp(200, body))
    got = _run(cd._prefetch_quotes_bulk(client, ["TCS", "INFY", "ZEROCO", "BADPX"]))
    assert set(got) == {"TCS", "INFY"}
    assert got["TCS"]["previous_close"] == 98.0


def test_prefetch_returns_empty_on_http_error_exception_and_bad_json():
    assert _run(cd._prefetch_quotes_bulk(_PostClient(_Resp(500, {}, "boom")), ["A"])) == {}
    assert _run(cd._prefetch_quotes_bulk(_PostClient(exc=RuntimeError("down")), ["A"])) == {}
    assert _run(cd._prefetch_quotes_bulk(_PostClient(_Resp(200, ValueError("bad"))), ["A"])) == {}
    assert _run(cd._prefetch_quotes_bulk(_PostClient(_Resp(200, ["not a dict"])), ["A"])) == {}
    assert _run(cd._prefetch_quotes_bulk(_PostClient(_Resp(200, {})), [])) == {}


def test_bulk_quote_for_lookup_and_switch(monkeypatch):
    pre = {"TCS": {"price": 1}}
    assert cd._bulk_quote_for(pre, "tcs.NS") == {"price": 1}
    assert cd._bulk_quote_for(pre, "NOPE") is None
    assert cd._bulk_quote_for(None, "TCS") is None
    assert cd._bulk_quote_for({}, "TCS") is None
    assert cd._bulk_quote_for("junk", "TCS") is None
    monkeypatch.setattr(cd, "USE_BULK_QUOTES", False)
    assert cd._bulk_quote_for(pre, "TCS") is None


# ── analyses skip GET /quote when handed a quote ────────────────────────────

def test_volume_shock_analysis_uses_the_given_quote_without_get_quote(monkeypatch):
    async def boom(client, symbol):
        raise AssertionError("GET /quote must not be called")

    monkeypatch.setattr(cd, "_fetch_quote", boom)
    # price 100 vs previous close 100 -> 0% return, far below the gate: rejected on the quote alone
    res = _run(cd._volume_shock_analysis(object(), "TCS", {"price": 100.0, "previous_close": 100.0}))
    assert "quote pre-check" in res["reject_reason"]


def test_volume_shock_analysis_still_asks_get_quote_when_none_is_given(monkeypatch):
    seen = []

    async def fq(client, symbol):
        seen.append(symbol)
        return {"price": 100.0, "previous_close": 100.0}

    monkeypatch.setattr(cd, "_fetch_quote", fq)
    res = _run(cd._volume_shock_analysis(object(), "TCS"))
    assert seen == ["TCS"] and "quote pre-check" in res["reject_reason"]


def test_multi_tf_analysis_uses_the_given_quote_without_get_quote(monkeypatch):
    async def boom(client, symbol):
        raise AssertionError("GET /quote must not be called")

    async def no_hist(client, symbol, period, interval="1d"):
        return []

    monkeypatch.setattr(cd, "_fetch_quote", boom)
    monkeypatch.setattr(cd, "_fetch_history", no_hist)
    res = _run(cd._multi_tf_analysis(object(), "TCS", {"price": 100.0, "previous_close": 99.0}))
    assert isinstance(res, dict)


# ── /history pacing ─────────────────────────────────────────────────────────

class _HistClient:
    def __init__(self):
        self.now = self.peak = 0

    async def get(self, url, params=None, timeout=None):
        self.now += 1
        self.peak = max(self.peak, self.now)
        await asyncio.sleep(0.01)
        self.now -= 1
        return _Resp(200, {"candles": [{"close": 1}]})


def test_history_calls_are_capped_in_flight(monkeypatch):
    monkeypatch.setattr(cd, "HISTORY_MAX_INFLIGHT", 3)
    cd._HISTORY_GATES.clear()
    client = _HistClient()

    async def go():
        return await asyncio.gather(*[cd._fetch_history_raw(client, f"S{i}", "1mo", "1d") for i in range(30)])

    out = _run(go())
    assert len(out) == 30 and all(out)
    assert client.peak == 3


def test_history_pacing_off_means_no_cap(monkeypatch):
    monkeypatch.setattr(cd, "HISTORY_MAX_INFLIGHT", 0)
    client = _HistClient()

    async def go():
        return await asyncio.gather(*[cd._fetch_history_raw(client, f"S{i}", "1mo", "1d") for i in range(12)])

    _run(go())
    assert client.peak == 12
    assert cd._history_gate() is None            # no running loop either


def test_history_gate_is_per_loop_and_bounded(monkeypatch):
    monkeypatch.setattr(cd, "HISTORY_MAX_INFLIGHT", 2)
    cd._HISTORY_GATES.clear()

    async def one():
        return cd._history_gate()

    a = _run(one())
    assert isinstance(a, asyncio.Semaphore)
    for _ in range(12):                           # many loops (tests) never grow the dict without bound
        _run(one())
    assert len(cd._HISTORY_GATES) <= 9


# ── small batches go bulk-first ─────────────────────────────────────────────

class _Dummy:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture()
def feed_env(monkeypatch):
    f.clear_priority_share()
    f.clear_dead_symbols()
    monkeypatch.setattr(f.httpx, "AsyncClient", _Dummy)
    monkeypatch.setattr(f, "FEED_BULK_MIN_SYMBOLS", 25)
    monkeypatch.setattr(f, "FEED_SMALL_BATCH_BULK_MIN_SYMBOLS", 5)
    calls = {"bulk": [], "single": []}

    async def fake_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None, schedule_atr=True):
        calls["bulk"].append(list(symbols))
        if stats is not None:
            stats.setdefault("failed", 0)
            stats.setdefault("reasons", {})
        from tests.test_group225_priority_lane_backpressure import _tick
        return {f._clean_sym(s): _tick(f._clean_sym(s), 77.0, "bulk(t)") for s in symbols}

    async def fake_get_quote(client, symbol, **kw):
        calls["single"].append(symbol)
        from tests.test_group225_priority_lane_backpressure import _tick
        return _tick(symbol, 50.0, "single")

    monkeypatch.setattr(f, "_bulk_ticks", fake_bulk)
    monkeypatch.setattr(f, "get_quote", fake_get_quote)
    yield calls
    f.clear_priority_share()


def test_twenty_entry_candidates_are_priced_by_one_bulk_call(feed_env):
    out = _run(f.get_quotes([f"S{i}" for i in range(20)]))
    assert len(out) == 20
    assert len(feed_env["bulk"]) == 1 and len(feed_env["bulk"][0]) == 20
    assert feed_env["single"] == []


def test_batches_below_the_small_limit_keep_the_per_symbol_path(feed_env):
    out = _run(f.get_quotes(["A", "B", "C", "D"]))
    assert len(out) == 4 and feed_env["bulk"] == [] and len(feed_env["single"]) == 4


def test_the_small_batch_limit_is_inclusive_at_its_value(feed_env):
    _run(f.get_quotes(["A", "B", "C", "D", "E"]))
    assert len(feed_env["bulk"]) == 1 and feed_env["single"] == []


def test_small_batch_bulk_can_be_switched_off(feed_env, monkeypatch):
    monkeypatch.setattr(f, "FEED_SMALL_BATCH_BULK_MIN_SYMBOLS", 26)
    _run(f.get_quotes([f"S{i}" for i in range(20)]))
    assert feed_env["bulk"] == [] and len(feed_env["single"]) == 20


def test_history_gate_without_a_running_loop_is_none(monkeypatch):
    monkeypatch.setattr(cd, "HISTORY_MAX_INFLIGHT", 4)
    assert cd._history_gate() is None


# ── orchestration: both candidate tracks hand the bulk quote to the analysis ─────────────

def test_standard_track_passes_the_bulk_quote_and_falls_back_without_one(db, monkeypatch):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda d: set())
    cd.clear_reject_cache()

    async def fake_fetch(client, path):
        if path == cd._SOURCES["hot_picks"]:
            return {"bulk_insider_driven": [
                {"symbol": s, "decision": "BUY NOW", "score": 80.0, "price": 100.0} for s in ("HASQ", "NOQ")]}
        return None

    async def fake_prefetch(client, symbols):
        return {"HASQ": {"symbol": "HASQ", "price": 101.0}}

    seen = {}

    async def fake_mtf(client, symbol, quote=None):
        seen[symbol] = quote
        return {"reject_reason": "weak", "atr_pct": None}

    async def fake_mcap(client, symbol):
        return 5000.0

    monkeypatch.setattr(cd, "_fetch", fake_fetch)
    monkeypatch.setattr(cd, "_prefetch_quotes_bulk", fake_prefetch)
    monkeypatch.setattr(cd, "_multi_tf_analysis", fake_mtf)
    monkeypatch.setattr(cd, "_fetch_market_cap_cr", fake_mcap)
    _run(cd._refresh_standard_candidates(db, "REAL", set()))
    assert seen == {"HASQ": {"symbol": "HASQ", "price": 101.0}, "NOQ": None}


def test_volume_shock_track_passes_the_bulk_quote_and_logs_the_split(db, monkeypatch, caplog):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda d: set())
    monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", False)

    async def fake_universe(client):
        return ["HASQ", "NOQ"]

    async def fake_prefetch(client, symbols):
        return {"HASQ": {"symbol": "HASQ", "price": 101.0}}

    seen = {}

    async def fake_vs(client, symbol, quote=None):
        seen[symbol] = quote
        return {"reject_reason": "Today's return 0.1% < 2.5% volume-shock breakout threshold.", "atr_pct": None}

    monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
    monkeypatch.setattr(cd, "_prefetch_quotes_bulk", fake_prefetch)
    monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)
    with caplog.at_level("INFO"):
        _run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
    assert seen == {"HASQ": {"symbol": "HASQ", "price": 101.0}, "NOQ": None}
    assert "volume_shock priced 1 of 2 symbol(s) from the bulk answer; 1 fall back to GET /quote" in caplog.text


def test_volume_shock_track_is_quiet_about_the_split_when_switched_off(db, monkeypatch, caplog):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda d: set())
    monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", False)
    monkeypatch.setattr(cd, "USE_BULK_QUOTES", False)

    async def fake_universe(client):
        return ["HASQ"]

    async def fake_prefetch(client, symbols):
        return {"HASQ": {"symbol": "HASQ", "price": 101.0}}

    seen = {}

    async def fake_vs(client, symbol, quote=None):
        seen[symbol] = quote
        return {"reject_reason": "No quote available for volume-shock check.", "atr_pct": None}

    monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
    monkeypatch.setattr(cd, "_prefetch_quotes_bulk", fake_prefetch)
    monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)
    with caplog.at_level("INFO"):
        _run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
    assert seen == {"HASQ": None}
    assert "from the bulk answer" not in caplog.text
