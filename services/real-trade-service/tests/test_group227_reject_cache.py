"""group227: rejection cache for the standard candidate track (item 2 of the 2026-10-07 10:33 IST log review).

A symbol rejected for a stable reason must not cost 7 /history calls + a quote + a market-cap call every 3-minute cycle.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import intraday_eligibility
import models
from candidate_engine import candidates as cd

_engine = create_engine("sqlite:///:memory:")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cd.clear_reject_cache()
    saved = cd._adaptive_max_atr_pct
    yield
    cd._adaptive_max_atr_pct = saved
    cd.clear_reject_cache()


def _rej(kind, **extra):
    d = {"reject_reason": f"rejected: {kind}", "tf_returns": {}, "bullish_count": 0, "atr_pct": None,
         "reject_kind": kind}
    d.update(extra)
    return d


# ── cache unit behaviour ─────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["price_floor", "downtrend_6m", "atr", "volume", "bullish_score", "range_52w", "resistance"])
def test_definite_rejections_are_cached(kind):
    extra = {"atr_pct": 99.0} if kind == "atr" else {}
    cd._reject_cache_put("AAA", _rej(kind, **extra))
    got = cd._reject_cache_get("AAA")
    assert got and got["from_reject_cache"] is True and got["reject_kind"] == kind
    assert got["reject_reason"] == f"rejected: {kind}"
    assert "reject_cache_age_s" in got


def test_passes_and_data_gaps_are_never_cached():
    cd._reject_cache_put("PASS", {"reject_reason": None, "tf_returns": {}})
    cd._reject_cache_put("STARVED", {"reject_reason": "x", "data_starved": True, "reject_kind": "volume"})
    cd._reject_cache_put("INCOMPLETE", {"reject_reason": "x", "data_incomplete": True, "reject_kind": "bullish_score"})
    cd._reject_cache_put("NOQUOTE", {"reject_reason": "No live quote available"})          # no reject_kind
    cd._reject_cache_put("ERR", {"reject_reason": "MTF fetch error: boom", "atr_pct": None})
    cd._reject_cache_put("NOTDICT", None)
    cd._reject_cache_put("CACHED", _rej("volume", from_reject_cache=True))
    for sym in ("PASS", "STARVED", "INCOMPLETE", "NOQUOTE", "ERR", "NOTDICT", "CACHED"):
        assert cd._reject_cache_get(sym) is None


def test_stable_and_price_ttls_differ(monkeypatch):
    monkeypatch.setattr(cd, "REJECT_CACHE_STABLE_S", 3600.0)
    monkeypatch.setattr(cd, "REJECT_CACHE_PRICE_S", 900.0)
    cd._reject_cache_put("STABLE", _rej("downtrend_6m"))
    cd._reject_cache_put("PRICEY", _rej("range_52w"))
    t0 = cd._REJECT_CACHE["STABLE"][0]
    now = {"t": t0 + 1000}
    monkeypatch.setattr(cd.time, "monotonic", lambda: now["t"])
    assert cd._reject_cache_get("STABLE") is not None       # 1000s < 3600s
    assert cd._reject_cache_get("PRICEY") is None           # 1000s > 900s -> expired and dropped
    assert "PRICEY" not in cd._REJECT_CACHE
    now["t"] = t0 + 3601
    assert cd._reject_cache_get("STABLE") is None


def test_ttl_zero_turns_the_cache_off(monkeypatch):
    monkeypatch.setattr(cd, "REJECT_CACHE_STABLE_S", 0.0)
    monkeypatch.setattr(cd, "REJECT_CACHE_PRICE_S", 0.0)
    cd._reject_cache_put("AAA", _rej("downtrend_6m"))
    cd._reject_cache_put("BBB", _rej("range_52w"))
    assert cd._reject_cache_get("AAA") is None and cd._reject_cache_get("BBB") is None


def test_entry_from_a_previous_ist_day_is_dropped():
    cd._reject_cache_put("AAA", _rej("downtrend_6m"))
    ts, _day, res = cd._REJECT_CACHE["AAA"]
    cd._REJECT_CACHE["AAA"] = (ts, "2000-01-01", res)
    assert cd._reject_cache_get("AAA") is None
    assert "AAA" not in cd._REJECT_CACHE


def test_atr_rejection_is_dropped_when_the_adaptive_cap_rises_past_it():
    cd._adaptive_max_atr_pct = 5.0
    cd._reject_cache_put("VOLATILE", _rej("atr", atr_pct=6.5))
    assert cd._reject_cache_get("VOLATILE") is not None
    cd._adaptive_max_atr_pct = 7.0                      # cap now above the cached ATR: judge again
    assert cd._reject_cache_get("VOLATILE") is None
    assert "VOLATILE" not in cd._REJECT_CACHE
    cd._reject_cache_put("NOATR", _rej("atr", atr_pct=None))
    assert cd._reject_cache_get("NOATR") is None


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(cd, "_REJECT_CACHE_MAX", 3)
    for i in range(3):
        cd._reject_cache_put(f"S{i}", _rej("volume"))
    cd._reject_cache_put("S3", _rej("volume"))
    assert list(cd._REJECT_CACHE) == ["S3"]


def test_bad_ttl_env_falls_back(monkeypatch):
    monkeypatch.setenv("X_TTL", "abc")
    assert cd._reject_ttl_env("X_TTL", "42") == 42.0
    monkeypatch.setenv("X_TTL", " 7 ")
    assert cd._reject_ttl_env("X_TTL", "42") == 7.0


def test_clear_history_state_also_clears_the_reject_cache():
    cd._reject_cache_put("AAA", _rej("volume"))
    cd.clear_history_state()
    assert cd._reject_cache_get("AAA") is None


def test_helpers_never_raise_on_corrupt_state(monkeypatch):
    cd._REJECT_CACHE["BAD"] = "not-a-tuple"
    assert cd._reject_cache_get("BAD") is None
    monkeypatch.setattr(cd, "_REJECT_CACHE", None)       # .get on None raises inside the guard
    assert cd._reject_cache_get("X") is None
    cd._reject_cache_put("X", _rej("volume"))            # len(None) raises inside the guard
    monkeypatch.undo()                                   # restore the dict before the autouse fixture's teardown


# ── _multi_tf_analysis tags its verdicts ─────────────────────────────────────
def _candles(n, start, end, vol=1000.0):
    step = (end - start) / max(n - 1, 1)
    return [{"open": start + i * step, "high": start + i * step + 1, "low": start + i * step - 1,
             "close": start + i * step, "volume": vol} for i in range(n)]


def _patch_fetches(monkeypatch, hist, quote):
    async def fake_hist(client, symbol, period, interval="1d"):
        return hist(period, interval)

    async def fake_quote(client, symbol):
        return quote
    monkeypatch.setattr(cd, "_fetch_history", fake_hist)
    monkeypatch.setattr(cd, "_fetch_quote", fake_quote)


def test_downtrend_and_price_floor_are_tagged(monkeypatch):
    _patch_fetches(monkeypatch, lambda p, i: _candles(30, 200, 100), {"price": 100.0})
    res = run(cd._multi_tf_analysis(object(), "DOWN"))
    assert res["reject_kind"] == "downtrend_6m"
    _patch_fetches(monkeypatch, lambda p, i: _candles(30, 5, 6), {"price": 6.0})
    res = run(cd._multi_tf_analysis(object(), "PENNY"))
    assert res["reject_kind"] == "price_floor"


def test_weak_momentum_is_tagged_bullish_score_and_incomplete_is_not(monkeypatch):
    _patch_fetches(monkeypatch, lambda p, i: _candles(30, 100, 100.0), {"price": 100.0})
    res = run(cd._multi_tf_analysis(object(), "FLAT"))
    assert res["reject_kind"] == "bullish_score" and not res.get("data_incomplete")
    # most horizons missing but enough weight left to still qualify -> "cannot judge", no kind, never cached
    _patch_fetches(monkeypatch, lambda p, i: _candles(30, 100, 130) if p in ("1y", "2y") else [], {"price": 130.0})
    res = run(cd._multi_tf_analysis(object(), "GAPPY"))
    if res.get("data_incomplete"):
        assert "reject_kind" not in res


# ── orchestration: cached symbols skip every upstream call ───────────────────
def _setup_cycle(monkeypatch, rows_syms, verdicts, mcap=5000.0):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda db: set())
    calls = {"mtf": [], "mcap": [], "prefetch": []}

    async def fake_fetch(client, path):
        if path == cd._SOURCES["hot_picks"]:
            return {"bulk_insider_driven": [
                {"symbol": s, "decision": "BUY NOW", "score": 80.0, "price": 100.0} for s in rows_syms]}
        return None

    async def fake_prefetch(client, symbols):
        calls["prefetch"].append(list(symbols))

    async def fake_mtf(client, symbol):
        calls["mtf"].append(symbol)
        return dict(verdicts[symbol])

    async def fake_mcap(client, symbol):
        calls["mcap"].append(symbol)
        return mcap

    monkeypatch.setattr(cd, "_fetch", fake_fetch)
    monkeypatch.setattr(cd, "_prefetch_quotes_bulk", fake_prefetch)
    monkeypatch.setattr(cd, "_multi_tf_analysis", fake_mtf)
    monkeypatch.setattr(cd, "_fetch_market_cap_cr", fake_mcap)
    return calls


PASS = {"reject_reason": None, "bullish_count": 5.0, "tf_returns": {}, "atr_pct": None, "market_note": ""}


def test_second_cycle_skips_history_quote_and_mcap_for_cached_rejections(db, monkeypatch, caplog):
    verdicts = {"STALE1": _rej("downtrend_6m"), "STALE2": _rej("range_52w"), "GOOD": PASS}
    calls = _setup_cycle(monkeypatch, ["STALE1", "STALE2", "GOOD"], verdicts)

    inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", set()))
    assert inserted == 1 and seen == {"STALE1", "STALE2", "GOOD"}
    assert sorted(calls["mtf"]) == ["GOOD", "STALE1", "STALE2"]
    assert sorted(calls["mcap"]) == ["GOOD", "STALE1", "STALE2"]

    # second cycle: only GOOD is analysed again (and is excluded here, as the real cycle's cooldown would)
    calls["mtf"].clear(); calls["mcap"].clear(); calls["prefetch"].clear()
    caplog.clear()
    with caplog.at_level("INFO"):
        inserted2, seen2 = run(cd._refresh_standard_candidates(db, "REAL", set()))
    assert calls["mtf"] == ["GOOD"] and calls["mcap"] == ["GOOD"]
    assert calls["prefetch"] == [["GOOD"]]
    assert seen2 == {"STALE1", "STALE2", "GOOD"}
    text = caplog.text
    assert "2 of 3 symbol(s) answered from the rejection cache" in text
    assert "CANDIDATE REJECTED STALE1" not in text           # repeat rejections stay out of the INFO stream


def test_cached_rejection_logs_at_debug_with_age(db, monkeypatch, caplog):
    calls = _setup_cycle(monkeypatch, ["STALE1"], {"STALE1": _rej("volume")})
    run(cd._refresh_standard_candidates(db, "REAL", set()))
    caplog.clear()
    with caplog.at_level("DEBUG"):
        run(cd._refresh_standard_candidates(db, "REAL", set()))
    assert calls["mtf"] == ["STALE1"]                         # only the first cycle called it
    assert any("cached" in r.message and "STALE1" in r.message and r.levelname == "DEBUG" for r in caplog.records)


def test_symbols_with_data_gaps_are_reanalysed_every_cycle(db, monkeypatch):
    starved = {"reject_reason": "No quote or history", "data_starved": True, "atr_pct": None, "tf_returns": {}}
    calls = _setup_cycle(monkeypatch, ["GAP"], {"GAP": starved})
    run(cd._refresh_standard_candidates(db, "REAL", set()))
    run(cd._refresh_standard_candidates(db, "REAL", set()))
    assert calls["mtf"] == ["GAP", "GAP"]


def test_all_symbols_cached_means_no_upstream_calls_and_no_starved_warning(db, monkeypatch, caplog):
    calls = _setup_cycle(monkeypatch, ["A", "B", "C"], {s: _rej("downtrend_6m") for s in "ABC"})
    run(cd._refresh_standard_candidates(db, "REAL", set()))
    calls["mtf"].clear(); calls["mcap"].clear(); calls["prefetch"].clear()
    with caplog.at_level("WARNING"):
        inserted, _ = run(cd._refresh_standard_candidates(db, "REAL", set()))
    assert inserted == 0 and calls["mtf"] == [] and calls["mcap"] == []
    assert calls["prefetch"] == [[]]
    assert "zero quote/history data" not in caplog.text


def test_cache_off_restores_reanalysis_every_cycle(db, monkeypatch):
    monkeypatch.setattr(cd, "REJECT_CACHE_STABLE_S", 0.0)
    monkeypatch.setattr(cd, "REJECT_CACHE_PRICE_S", 0.0)
    calls = _setup_cycle(monkeypatch, ["A"], {"A": _rej("downtrend_6m")})
    run(cd._refresh_standard_candidates(db, "REAL", set()))
    run(cd._refresh_standard_candidates(db, "REAL", set()))
    assert calls["mtf"] == ["A", "A"]


def test_mtf_exception_result_is_not_cached(db, monkeypatch):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda db: set())

    async def fake_fetch(client, path):
        if path == cd._SOURCES["hot_picks"]:
            return {"bulk_insider_driven": [{"symbol": "BOOM", "decision": "BUY NOW", "score": 80.0, "price": 1.0}]}
        return None

    async def fake_prefetch(client, symbols):
        return None

    async def boom(client, symbol):
        raise RuntimeError("x")

    async def fake_mcap(client, symbol):
        return 5000.0
    monkeypatch.setattr(cd, "_fetch", fake_fetch)
    monkeypatch.setattr(cd, "_prefetch_quotes_bulk", fake_prefetch)
    monkeypatch.setattr(cd, "_multi_tf_analysis", boom)
    monkeypatch.setattr(cd, "_fetch_market_cap_cr", fake_mcap)
    run(cd._refresh_standard_candidates(db, "REAL", set()))
    assert cd._reject_cache_get("BOOM") is None
