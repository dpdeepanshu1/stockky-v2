"""
tests/test_candidates_helpers.py

100%-coverage-plan follow-up: candidate_engine/candidates.py -- 0% coverage,
2077 statements, biggest remaining gap in the plan. This is round 1,
mirroring the two-round approach already used for execution/auto_pilot.py
(session85/86): target the self-contained pieces first — pure computation
helpers, the row-normalization functions per source, the HTTP fetch
helpers (mocked httpx client), the quality gate, the sector-peer-history
cache, the adaptive-param refresh, and the DB-backed dedupe-cooldown
lookup. The big cycle-orchestration functions this file builds on top of
those pieces (_multi_tf_analysis, _volume_shock_analysis,
_refresh_standard_candidates, _refresh_volume_shock_candidates,
refresh_candidates, _fetch_volume_shock_universe's caller context) are
deliberately left for a follow-up round — same reasoning as auto_pilot's
cycle-orchestration round: they need fixture-level mocking of several
chained network calls to exercise properly.

Run from services/real-trade-service:
    python3 -m pytest tests/test_candidates_helpers.py -q \
        --cov=candidate_engine.candidates --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
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
def _reset_module_globals():
    """_sector_peer_history and the _adaptive_* globals are process-local
    module state (by design — see the module docstring above them). Reset
    before and after every test so tests can't leak state into each
    other regardless of order."""
    saved = dict(
        sector_peer_history=dict(cd._sector_peer_history),
        adaptive_max_atr_pct=cd._adaptive_max_atr_pct,
        adaptive_max_atr_pct_source=cd._adaptive_max_atr_pct_source,
        adaptive_fund_floor=cd._adaptive_fund_floor,
        adaptive_tech_floor=cd._adaptive_tech_floor,
        adaptive_min_market_cap_cr=cd._adaptive_min_market_cap_cr,
        adaptive_rsi_oversold=cd._adaptive_rsi_oversold,
        adaptive_rsi_overbought=cd._adaptive_rsi_overbought,
        adaptive_extended_1m_pct=cd._adaptive_extended_1m_pct,
        adaptive_extended_short_pct=cd._adaptive_extended_short_pct,
        adaptive_trend_weight=cd._adaptive_trend_weight,
        adaptive_meanrev_weight=cd._adaptive_meanrev_weight,
        adaptive_params_source=cd._adaptive_params_source,
    )
    cd._sector_peer_history.clear()
    yield
    cd._sector_peer_history.clear()
    cd._sector_peer_history.update(saved["sector_peer_history"])
    cd._adaptive_max_atr_pct = saved["adaptive_max_atr_pct"]
    cd._adaptive_max_atr_pct_source = saved["adaptive_max_atr_pct_source"]
    cd._adaptive_fund_floor = saved["adaptive_fund_floor"]
    cd._adaptive_tech_floor = saved["adaptive_tech_floor"]
    cd._adaptive_min_market_cap_cr = saved["adaptive_min_market_cap_cr"]
    cd._adaptive_rsi_oversold = saved["adaptive_rsi_oversold"]
    cd._adaptive_rsi_overbought = saved["adaptive_rsi_overbought"]
    cd._adaptive_extended_1m_pct = saved["adaptive_extended_1m_pct"]
    cd._adaptive_extended_short_pct = saved["adaptive_extended_short_pct"]
    cd._adaptive_trend_weight = saved["adaptive_trend_weight"]
    cd._adaptive_meanrev_weight = saved["adaptive_meanrev_weight"]
    cd._adaptive_params_source = saved["adaptive_params_source"]


# ---------------------------------------------------------------------------
# Fake httpx.AsyncClient
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Routes .get()/.post() calls by URL substring to a canned response
    or an exception, recording every call for assertions."""

    def __init__(self, responses=None, raises=None):
        # responses: list of (url_substring, _FakeResponse) checked in order
        self.responses = responses or []
        # raises: list of (url_substring, Exception) checked in order, before responses
        self.raises = raises or []
        self.calls = []

    async def get(self, url, timeout=None, params=None):
        self.calls.append(("GET", url, params))
        return self._resolve(url)

    async def post(self, url, timeout=None, json=None):
        self.calls.append(("POST", url, json))
        return self._resolve(url)

    def _resolve(self, url):
        for sub, exc in self.raises:
            if sub in url:
                raise exc
        for sub, resp in self.responses:
            if sub in url:
                return resp
        # default: 404 not matched
        return _FakeResponse(status_code=404, payload=None, text="no route configured")


def _candles(n, start_price=100.0, step=1.0, volume=1000.0):
    """n OHLC candle dicts with monotonically increasing close (step) and
    a plausible high/low/open spread, for ATR/volume/resistance tests."""
    out = []
    price = start_price
    for i in range(n):
        o = price
        c = price + step
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        out.append({"open": o, "high": h, "low": l, "close": c, "volume": volume})
        price = c
    return out


# ---------------------------------------------------------------------------
# Sector peer history cache
# ---------------------------------------------------------------------------

class TestSectorPeerHistory:
    def test_record_noop_on_missing_sector_or_score(self):
        cd._record_sector_peer_score("", 50.0)
        cd._record_sector_peer_score("IT", None)
        assert cd._sector_peer_history == {}

    def test_record_and_retrieve_roundtrip(self):
        cd._record_sector_peer_score("IT", 60.0)
        cd._record_sector_peer_score("IT", 70.0)
        scores = cd._get_cross_cycle_peer_scores("IT")
        assert scores == [60.0, 70.0]

    def test_retrieve_empty_sector_name_returns_empty(self):
        assert cd._get_cross_cycle_peer_scores("") == []

    def test_retrieve_unknown_sector_returns_empty(self):
        assert cd._get_cross_cycle_peer_scores("NEVERSEEN") == []

    def test_record_prunes_stale_entries_past_max_age(self, monkeypatch):
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_HISTORY_MAX_AGE_MINUTES", 10)
        stale_ts = time.time() - 20 * 60  # 20 min ago, past the 10-min window
        cd._sector_peer_history["IT"] = [(stale_ts, 40.0)]
        cd._record_sector_peer_score("IT", 80.0)
        scores = cd._get_cross_cycle_peer_scores("IT")
        assert scores == [80.0]

    def test_record_caps_at_max_samples_dropping_oldest(self, monkeypatch):
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_HISTORY_MAX_SAMPLES", 2)
        cd._record_sector_peer_score("AUTO", 10.0)
        cd._record_sector_peer_score("AUTO", 20.0)
        cd._record_sector_peer_score("AUTO", 30.0)
        scores = cd._get_cross_cycle_peer_scores("AUTO")
        assert scores == [20.0, 30.0]

    def test_retrieve_also_prunes_stale_entries(self, monkeypatch):
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_HISTORY_MAX_AGE_MINUTES", 10)
        stale_ts = time.time() - 20 * 60
        fresh_ts = time.time()
        cd._sector_peer_history["BANK"] = [(stale_ts, 11.0), (fresh_ts, 99.0)]
        scores = cd._get_cross_cycle_peer_scores("BANK")
        assert scores == [99.0]
        # pruning should also have rewritten the stored bucket
        assert cd._sector_peer_history["BANK"] == [(fresh_ts, 99.0)]


# ---------------------------------------------------------------------------
# _refresh_cycle_adaptive_params
# ---------------------------------------------------------------------------

class TestRefreshCycleAdaptiveParams:
    def test_success_updates_all_globals(self, db, monkeypatch):
        monkeypatch.setattr(cd.amp, "adaptive_max_atr_pct", lambda db: (5.5, "atr_src"))
        monkeypatch.setattr(cd.amp, "adaptive_quality_floor", lambda db, base: (base + 1, "floor_src"))
        monkeypatch.setattr(cd.amp, "adaptive_min_market_cap_cr", lambda db: (2000.0, "mcap_src"))
        monkeypatch.setattr(cd.amp, "adaptive_rsi_bounds", lambda db: (25.0, 75.0, "rsi_src"))
        monkeypatch.setattr(cd.amp, "adaptive_extension_thresholds", lambda db: (0.2, 0.1, "ext_src"))
        monkeypatch.setattr(cd.amp, "adaptive_signal_weights", lambda db: (1.5, 0.5, "weight_src"))
        cd._refresh_cycle_adaptive_params(db)
        assert cd._adaptive_max_atr_pct == 5.5
        assert cd._adaptive_max_atr_pct_source == "atr_src"
        assert cd._adaptive_min_market_cap_cr == 2000.0
        assert cd._adaptive_rsi_oversold == 25.0
        assert cd._adaptive_rsi_overbought == 75.0
        assert cd._adaptive_extended_1m_pct == 0.2
        assert cd._adaptive_extended_short_pct == 0.1
        assert cd._adaptive_trend_weight == 1.5
        assert cd._adaptive_meanrev_weight == 0.5
        assert "atr=atr_src" in cd._adaptive_params_source

    def test_exception_keeps_prior_values(self, db, monkeypatch):
        cd._adaptive_max_atr_pct = 4.2
        monkeypatch.setattr(
            cd.amp, "adaptive_max_atr_pct",
            lambda db: (_ for _ in ()).throw(RuntimeError("amp down")),
        )
        cd._refresh_cycle_adaptive_params(db)  # must not raise
        assert cd._adaptive_max_atr_pct == 4.2


# ---------------------------------------------------------------------------
# _fetch / _fetch_history / _fetch_quote / _fetch_delivery
# ---------------------------------------------------------------------------

class TestFetch:
    def test_returns_json_on_200(self):
        client = _FakeAsyncClient(responses=[("/stockky-hot", _FakeResponse(200, {"a": 1}))])
        result = run(cd._fetch(client, "/stockky-hot"))
        assert result == {"a": 1}

    def test_returns_none_on_non_200(self):
        client = _FakeAsyncClient(responses=[("/stockky-hot", _FakeResponse(500, None, "boom"))])
        assert run(cd._fetch(client, "/stockky-hot")) is None

    def test_returns_none_on_exception(self):
        client = _FakeAsyncClient(raises=[("/stockky-hot", RuntimeError("network down"))])
        assert run(cd._fetch(client, "/stockky-hot")) is None


class TestFetchHistory:
    def test_returns_candles_on_200(self):
        client = _FakeAsyncClient(responses=[("/history/TCS", _FakeResponse(200, {"candles": [{"close": 1}]}))])
        result = run(cd._fetch_history(client, "TCS", "1y"))
        assert result == [{"close": 1}]

    def test_missing_candles_key_returns_empty_list(self):
        client = _FakeAsyncClient(responses=[("/history/TCS", _FakeResponse(200, {}))])
        assert run(cd._fetch_history(client, "TCS", "1y")) == []

    def test_non_200_returns_empty_list(self):
        client = _FakeAsyncClient(responses=[("/history/TCS", _FakeResponse(500))])
        assert run(cd._fetch_history(client, "TCS", "1y")) == []

    def test_exception_returns_empty_list(self):
        client = _FakeAsyncClient(raises=[("/history/TCS", RuntimeError("boom"))])
        assert run(cd._fetch_history(client, "TCS", "1y")) == []


class TestFetchQuote:
    def test_returns_json_on_200(self):
        client = _FakeAsyncClient(responses=[("/quote/TCS", _FakeResponse(200, {"price": 100}))])
        assert run(cd._fetch_quote(client, "TCS")) == {"price": 100}

    def test_non_200_returns_none_and_logs(self, caplog):
        import logging
        client = _FakeAsyncClient(responses=[("/quote/TCS", _FakeResponse(404, None, "not found"))])
        with caplog.at_level(logging.WARNING, logger="real-trade-candidates"):
            result = run(cd._fetch_quote(client, "TCS"))
        assert result is None
        assert any("market-data-service returned" in r.message for r in caplog.records)

    def test_exception_returns_none(self):
        client = _FakeAsyncClient(raises=[("/quote/TCS", RuntimeError("timeout"))])
        assert run(cd._fetch_quote(client, "TCS")) is None


class TestFetchDelivery:
    def test_returns_json_on_200(self):
        client = _FakeAsyncClient(responses=[("/delivery/TCS", _FakeResponse(200, {"delivery_pct": 55.0}))])
        assert run(cd._fetch_delivery(client, "TCS")) == {"delivery_pct": 55.0}

    def test_non_200_returns_none(self):
        client = _FakeAsyncClient(responses=[("/delivery/TCS", _FakeResponse(500))])
        assert run(cd._fetch_delivery(client, "TCS")) is None

    def test_exception_returns_none(self):
        client = _FakeAsyncClient(raises=[("/delivery/TCS", RuntimeError("boom"))])
        assert run(cd._fetch_delivery(client, "TCS")) is None


# ---------------------------------------------------------------------------
# _fetch_fund_tech_score
# ---------------------------------------------------------------------------

class TestFetchFundTechScore:
    def test_both_succeed(self):
        client = _FakeAsyncClient(responses=[
            (f"{config.FUNDAMENTAL_URL}/analyze/TCS", _FakeResponse(200, {
                "fundamental_score": 70, "sector_normalized": "IT", "market_cap": 5e11,
            })),
            (f"{config.TECHNICAL_URL}/analyze/TCS", _FakeResponse(200, {
                "technical_score": 65, "adx": 28,
            })),
        ])
        result = run(cd._fetch_fund_tech_score(client, "TCS"))
        assert result["fundamental_score"] == 70
        assert result["technical_score"] == 65
        assert result["sector"] == "IT"
        assert result["market_cap_cr"] == 5e11 / 1e7
        assert result["adx"] == 28

    def test_market_cap_falls_back_to_raw_dict(self):
        client = _FakeAsyncClient(responses=[
            (f"{config.FUNDAMENTAL_URL}/analyze/TCS", _FakeResponse(200, {
                "fundamental_score": 70, "raw": {"market_cap": 1e9},
            })),
        ])
        result = run(cd._fetch_fund_tech_score(client, "TCS"))
        assert result["market_cap_cr"] == 1e9 / 1e7

    def test_fundamental_fetch_exception_is_non_fatal(self):
        client = _FakeAsyncClient(
            raises=[(f"{config.FUNDAMENTAL_URL}/analyze/TCS", RuntimeError("fund down"))],
            responses=[(f"{config.TECHNICAL_URL}/analyze/TCS", _FakeResponse(200, {"technical_score": 60}))],
        )
        result = run(cd._fetch_fund_tech_score(client, "TCS"))
        assert result["fundamental_score"] is None
        assert result["technical_score"] == 60

    def test_technical_fetch_exception_is_non_fatal(self):
        client = _FakeAsyncClient(
            responses=[(f"{config.FUNDAMENTAL_URL}/analyze/TCS", _FakeResponse(200, {"fundamental_score": 60}))],
            raises=[(f"{config.TECHNICAL_URL}/analyze/TCS", RuntimeError("tech down"))],
        )
        result = run(cd._fetch_fund_tech_score(client, "TCS"))
        assert result["fundamental_score"] == 60
        assert result["technical_score"] is None

    def test_bad_market_cap_value_is_swallowed(self):
        client = _FakeAsyncClient(responses=[
            (f"{config.FUNDAMENTAL_URL}/analyze/TCS", _FakeResponse(200, {
                "fundamental_score": 70, "market_cap": "not-a-number",
            })),
        ])
        result = run(cd._fetch_fund_tech_score(client, "TCS"))
        assert result["market_cap_cr"] is None


class TestFetchMarketCapCr:
    def test_success(self):
        client = _FakeAsyncClient(responses=[
            (f"{config.FUNDAMENTAL_URL}/analyze/TCS", _FakeResponse(200, {"market_cap": 2e9})),
        ])
        assert run(cd._fetch_market_cap_cr(client, "TCS")) == 2e9 / 1e7

    def test_raw_fallback(self):
        client = _FakeAsyncClient(responses=[
            (f"{config.FUNDAMENTAL_URL}/analyze/TCS", _FakeResponse(200, {"raw": {"market_cap": 3e9}})),
        ])
        assert run(cd._fetch_market_cap_cr(client, "TCS")) == 3e9 / 1e7

    def test_non_200_returns_none(self):
        client = _FakeAsyncClient(responses=[(f"{config.FUNDAMENTAL_URL}/analyze/TCS", _FakeResponse(500))])
        assert run(cd._fetch_market_cap_cr(client, "TCS")) is None

    def test_missing_market_cap_returns_none(self):
        client = _FakeAsyncClient(responses=[(f"{config.FUNDAMENTAL_URL}/analyze/TCS", _FakeResponse(200, {}))])
        assert run(cd._fetch_market_cap_cr(client, "TCS")) is None

    def test_exception_returns_none(self):
        client = _FakeAsyncClient(raises=[(f"{config.FUNDAMENTAL_URL}/analyze/TCS", RuntimeError("boom"))])
        assert run(cd._fetch_market_cap_cr(client, "TCS")) is None


# ---------------------------------------------------------------------------
# _prefetch_quotes_bulk
# ---------------------------------------------------------------------------

class TestPrefetchQuotesBulk:
    def test_empty_symbol_list_is_noop(self):
        client = _FakeAsyncClient()
        run(cd._prefetch_quotes_bulk(client, []))
        assert client.calls == []

    def test_all_falsy_symbols_is_noop(self):
        client = _FakeAsyncClient()
        run(cd._prefetch_quotes_bulk(client, [None, "", None]))
        assert client.calls == []

    def test_posts_one_chunk_for_small_symbol_list(self):
        client = _FakeAsyncClient(responses=[("/quotes/bulk", _FakeResponse(200, {"ok": True}))])
        run(cd._prefetch_quotes_bulk(client, ["TCS", "INFY", "TCS"]))  # dup dropped
        assert len(client.calls) == 1
        assert client.calls[0][0] == "POST"
        assert client.calls[0][2]["symbols"] == ["TCS", "INFY"]

    def test_splits_into_multiple_chunks(self, monkeypatch):
        monkeypatch.setattr(cd, "BULK_QUOTE_CHUNK_SIZE", 2)
        client = _FakeAsyncClient(responses=[("/quotes/bulk", _FakeResponse(200, {}))])
        run(cd._prefetch_quotes_bulk(client, ["A", "B", "C", "D", "E"]))
        assert len(client.calls) == 3  # ceil(5/2)

    def test_chunk_error_status_is_logged_not_raised(self, caplog):
        import logging
        client = _FakeAsyncClient(responses=[("/quotes/bulk", _FakeResponse(500, None, "server error"))])
        with caplog.at_level(logging.WARNING, logger="real-trade-candidates"):
            run(cd._prefetch_quotes_bulk(client, ["TCS"]))  # must not raise
        assert any("got HTTP 500" in r.message for r in caplog.records)

    def test_chunk_exception_is_logged_not_raised(self, caplog):
        import logging
        client = _FakeAsyncClient(raises=[("/quotes/bulk", RuntimeError("conn reset"))])
        with caplog.at_level(logging.WARNING, logger="real-trade-candidates"):
            run(cd._prefetch_quotes_bulk(client, ["TCS"]))  # must not raise
        assert any("failed:" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _quality_gate_fund_tech
# ---------------------------------------------------------------------------

class TestQualityGateFundTech:
    def test_fundamental_below_floor_rejects(self, monkeypatch):
        monkeypatch.setattr(cd, "_adaptive_fund_floor", 40.0)
        passed, note = cd._quality_gate_fund_tech({"fundamental_score": 30}, [])
        assert passed is False
        assert "fundamental_score 30" in note

    def test_technical_below_floor_rejects(self, monkeypatch):
        monkeypatch.setattr(cd, "_adaptive_tech_floor", 40.0)
        passed, note = cd._quality_gate_fund_tech({"technical_score": 20}, [])
        assert passed is False
        assert "technical_score 20" in note

    def test_market_cap_below_floor_rejects(self, monkeypatch):
        monkeypatch.setattr(cd, "_adaptive_min_market_cap_cr", 1000.0)
        passed, note = cd._quality_gate_fund_tech({"market_cap_cr": 100.0}, [])
        assert passed is False
        assert "market_cap" in note

    def test_no_quality_data_passes_with_skip_note(self):
        passed, note = cd._quality_gate_fund_tech({}, [])
        assert passed is True
        assert "floor check skipped" in note

    def test_thin_sector_sample_passes_without_relative_check(self, monkeypatch):
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_MIN_PEERS", 5)
        passed, note = cd._quality_gate_fund_tech(
            {"fundamental_score": 60, "technical_score": 60},
            [{"fundamental_score": 55, "technical_score": 55}],
        )
        assert passed is True
        assert "too thin" in note

    def test_low_sector_percentile_rejects(self, monkeypatch):
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_MIN_PEERS", 2)
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_PCTL_FLOOR", 90.0)
        # keep the absolute floors low so a fundamental/technical_score of 20
        # clears them and we actually reach the sector-relative check below
        monkeypatch.setattr(cd, "_adaptive_fund_floor", 0.0)
        monkeypatch.setattr(cd, "_adaptive_tech_floor", 0.0)
        peers = [{"fundamental_score": 90, "technical_score": 90} for _ in range(4)]
        passed, note = cd._quality_gate_fund_tech(
            {"fundamental_score": 20, "technical_score": 20}, peers,
        )
        assert passed is False
        assert "sector-relative pctl" in note

    def test_good_sector_percentile_passes_with_mcap_tier_note(self, monkeypatch):
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_MIN_PEERS", 2)
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_PCTL_FLOOR", 10.0)
        peers = [{"fundamental_score": 10, "technical_score": 10} for _ in range(3)]
        passed, note = cd._quality_gate_fund_tech(
            {"fundamental_score": 90, "technical_score": 90, "market_cap_cr": 5000.0}, peers,
        )
        assert passed is True
        assert "sector pctl" in note
        assert "mcap=" in note

    def test_cross_cycle_peer_scores_merged_into_sample(self, monkeypatch):
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_MIN_PEERS", 3)
        # only 1 this-cycle peer -- alone too thin -- but +2 cross-cycle clears it
        passed, note = cd._quality_gate_fund_tech(
            {"fundamental_score": 60, "technical_score": 60},
            [{"fundamental_score": 55, "technical_score": 55}],
            cross_cycle_peer_scores=[50.0, 52.0],
        )
        assert "too thin" not in note
        assert "1 this-cycle + 2 recent-cycle" in note


# ---------------------------------------------------------------------------
# Pure analysis helpers
# ---------------------------------------------------------------------------

class TestComputeAtrFromCandles:
    def test_computes_value_with_enough_candles(self):
        candles = _candles(20)
        atr = cd._compute_atr_from_candles(candles)
        assert atr is not None
        assert atr > 0

    def test_none_with_too_few_candles(self):
        assert cd._compute_atr_from_candles(_candles(5)) is None

    def test_none_with_empty_list(self):
        assert cd._compute_atr_from_candles([]) is None

    def test_none_with_none_input(self):
        assert cd._compute_atr_from_candles(None) is None

    def test_none_when_all_candles_invalid(self):
        bad = [{"high": 0, "low": 0, "close": 0} for _ in range(20)]
        assert cd._compute_atr_from_candles(bad) is None

    def test_none_on_internal_exception(self):
        # non-dict entries -> AttributeError on .get(), caught -> None
        broken = ["not-a-dict"] * 20
        assert cd._compute_atr_from_candles(broken) is None


class TestPctReturn:
    def test_normal_computation(self):
        candles = [{"open": 100, "close": 100}, {"close": 110}]
        assert cd._pct_return(candles) == 10.0

    def test_too_few_candles_returns_none(self):
        assert cd._pct_return([{"open": 100, "close": 100}]) is None

    def test_falls_back_to_close_when_open_missing(self):
        candles = [{"close": 100}, {"close": 105}]
        assert cd._pct_return(candles) == 5.0

    def test_zero_first_returns_none(self):
        candles = [{"open": 0, "close": 0}, {"close": 105}]
        assert cd._pct_return(candles) is None


class TestWeightedBullishScoreAndIsBullish:
    def test_is_bullish_above_threshold(self):
        assert cd._is_bullish(cd.BULLISH_THRESHOLD_PCT + 0.1) is True

    def test_is_bullish_at_or_below_threshold(self):
        assert cd._is_bullish(cd.BULLISH_THRESHOLD_PCT) is False
        assert cd._is_bullish(0.0) is False

    def test_is_bullish_none(self):
        assert cd._is_bullish(None) is False

    def test_weighted_score_sums_only_bullish_timeframes_with_weights(self):
        tf_returns = {"1d": 1.0, "1w": 1.0, "1m": -2.0}
        score = cd._weighted_bullish_score(tf_returns)
        assert score == cd.TIMEFRAME_WEIGHTS["1d"] + cd.TIMEFRAME_WEIGHTS["1w"]

    def test_weighted_score_all_bearish_is_zero(self):
        assert cd._weighted_bullish_score({"1d": -1.0, "1w": -1.0}) == 0.0


class TestVolumeIsHealthy:
    def test_insufficient_data_fails_open(self):
        assert cd._volume_is_healthy(_candles(5)) is True

    def test_healthy_ratio_passes(self):
        candles = _candles(20, volume=1000.0)
        assert cd._volume_is_healthy(candles) is True

    def test_unhealthy_ratio_fails(self):
        candles = _candles(15, volume=1000.0)
        # tank the last 5 days' volume far below the 20-day average
        for c in candles[-5:]:
            c["volume"] = 1.0
        assert cd._volume_is_healthy(candles) is False

    def test_zero_avg20_fails_open(self):
        candles = [{"volume": 0} for _ in range(15)]
        assert cd._volume_is_healthy(candles) is True


class TestNearResistance:
    def test_insufficient_candles_returns_false(self):
        assert cd._near_resistance(_candles(5), 100.0) is False

    def test_non_positive_price_returns_false(self):
        assert cd._near_resistance(_candles(20), 0.0) is False

    def test_price_near_recent_high_is_true(self):
        candles = _candles(20, start_price=100.0, step=1.0)
        recent_high = max(c["high"] for c in candles[-20:])
        assert cd._near_resistance(candles, recent_high * 0.99) is True

    def test_price_far_from_recent_high_is_false(self):
        candles = _candles(20, start_price=100.0, step=1.0)
        assert cd._near_resistance(candles, 1.0) is False


# ---------------------------------------------------------------------------
# Row-normalization functions per source
# ---------------------------------------------------------------------------

class TestRowsFromHotPicks:
    def test_non_dict_payload_returns_empty(self):
        assert cd._rows_from_hot_picks(None) == []
        assert cd._rows_from_hot_picks([1, 2, 3]) == []

    def test_actionable_above_conviction_included(self):
        payload = {"bulk_insider_driven": [
            {"symbol": "tcs", "decision": "BUY NOW", "score": 80, "price": 3500},
        ]}
        rows = cd._rows_from_hot_picks(payload)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "TCS"
        assert rows[0]["source_tab"] == "hot_picks"
        assert rows[0]["conviction_score"] == 80.0
        assert rows[0]["signal_price"] == 3500

    def test_non_actionable_decision_excluded(self):
        payload = {"results_driven": [{"symbol": "TCS", "decision": "AVOID", "score": 90}]}
        assert cd._rows_from_hot_picks(payload) == []

    def test_below_conviction_excluded(self):
        payload = {"news_driven": [{"symbol": "TCS", "decision": "BUY NOW", "score": 1}]}
        assert cd._rows_from_hot_picks(payload) == []

    def test_missing_symbol_skipped(self):
        payload = {"bulk_insider_driven": [{"decision": "BUY NOW", "score": 90}]}
        assert cd._rows_from_hot_picks(payload) == []

    def test_all_three_buckets_scanned(self):
        payload = {
            "bulk_insider_driven": [{"symbol": "A", "decision": "BUY NOW", "score": 90}],
            "results_driven": [{"symbol": "B", "decision": "PREPARE TO BUY", "score": 90}],
            "news_driven": [{"symbol": "C", "decision": "BUY NOW", "score": 90}],
        }
        rows = cd._rows_from_hot_picks(payload)
        assert {r["symbol"] for r in rows} == {"A", "B", "C"}


class TestRowsFromIpo:
    def test_list_payload(self):
        payload = [{"symbol": "IPO1", "decision": "BUY NOW", "ipo_score": 80, "current_price": 100}]
        rows = cd._rows_from_ipo(payload)
        assert rows[0]["symbol"] == "IPO1"
        assert rows[0]["conviction_score"] == 80.0
        assert rows[0]["signal_price"] == 100

    def test_dict_with_results_key(self):
        payload = {"results": [{"symbol": "IPO2", "decision": "BUY NOW", "ipo_score": 90}]}
        assert len(cd._rows_from_ipo(payload)) == 1

    def test_dict_with_items_key_fallback(self):
        payload = {"items": [{"symbol": "IPO3", "decision": "BUY NOW", "ipo_score": 90}]}
        assert len(cd._rows_from_ipo(payload)) == 1

    def test_ipo_score_preferred_over_score_field(self):
        payload = [{"symbol": "IPO4", "decision": "BUY NOW", "ipo_score": 90, "score": 0}]
        rows = cd._rows_from_ipo(payload)
        assert rows[0]["conviction_score"] == 90.0

    def test_score_fallback_when_ipo_score_absent(self):
        payload = [{"symbol": "IPO5", "decision": "BUY NOW", "score": 70}]
        rows = cd._rows_from_ipo(payload)
        assert rows[0]["conviction_score"] == 70.0

    def test_signal_price_fallback_chain(self):
        payload = [{"symbol": "IPO6", "decision": "BUY NOW", "ipo_score": 90, "cmp": 55}]
        rows = cd._rows_from_ipo(payload)
        assert rows[0]["signal_price"] == 55

    def test_below_conviction_excluded(self):
        payload = [{"symbol": "IPO7", "decision": "BUY NOW", "ipo_score": 1}]
        assert cd._rows_from_ipo(payload) == []


class TestRowsFromVolumeShock:
    def test_non_dict_payload_returns_empty(self):
        assert cd._rows_from_volume_shock(None) == []

    def test_symbols_uppercased_and_stripped(self):
        payload = {"momentum_movers": [" tcs ", "infy"]}
        rows = cd._rows_from_volume_shock(payload)
        assert {r["symbol"] for r in rows} == {"TCS", "INFY"}
        assert all(r["conviction_score"] == cd.MIN_CONVICTION for r in rows)

    def test_empty_after_strip_skipped(self):
        payload = {"momentum_movers": ["   "]}
        assert cd._rows_from_volume_shock(payload) == []

    def test_non_string_entries_skipped(self):
        payload = {"momentum_movers": [123, None, "VALID"]}
        rows = cd._rows_from_volume_shock(payload)
        assert [r["symbol"] for r in rows] == ["VALID"]

    def test_no_momentum_movers_key_returns_empty(self):
        assert cd._rows_from_volume_shock({}) == []


class TestRowsFromSurprise:
    def test_no_payload_returns_empty(self):
        assert cd._rows_from_surprise(None) == []
        assert cd._rows_from_surprise({}) == []

    def test_list_payload(self):
        payload = [{"symbol": "SUR1", "score": 90}]
        rows = cd._rows_from_surprise(payload)
        assert rows[0]["symbol"] == "SUR1"
        assert rows[0]["decision_label"] == "BUY NOW"  # default fallback

    def test_stocks_key(self):
        payload = {"stocks": [{"symbol": "SUR2", "score": 90}]}
        assert len(cd._rows_from_surprise(payload)) == 1

    def test_results_key_fallback(self):
        payload = {"results": [{"symbol": "SUR3", "score": 90}]}
        assert len(cd._rows_from_surprise(payload)) == 1

    def test_no_decision_field_passes_purely_on_score(self):
        payload = [{"symbol": "SUR4", "score": 90}]  # no "decision" key at all
        assert len(cd._rows_from_surprise(payload)) == 1

    def test_present_but_non_actionable_decision_rejects_regardless_of_score(self):
        payload = [{"symbol": "SUR5", "decision": "AVOID", "score": 99}]
        assert cd._rows_from_surprise(payload) == []

    def test_surprise_score_field_used_as_fallback(self):
        payload = [{"symbol": "SUR6", "surprise_score": 90}]
        rows = cd._rows_from_surprise(payload)
        assert rows[0]["conviction_score"] == 90.0

    def test_below_conviction_excluded(self):
        payload = [{"symbol": "SUR7", "score": 1}]
        assert cd._rows_from_surprise(payload) == []

    def test_missing_symbol_skipped(self):
        payload = [{"score": 90}]
        assert cd._rows_from_surprise(payload) == []


# ---------------------------------------------------------------------------
# _fetch_volume_shock_universe
# ---------------------------------------------------------------------------

class TestFetchVolumeShockUniverse:
    def test_combines_fetch_and_row_normalization(self):
        client = _FakeAsyncClient(responses=[
            (cd._SOURCES["volume_shock"], _FakeResponse(200, {"momentum_movers": ["tcs", "infy"]})),
        ])
        result = run(cd._fetch_volume_shock_universe(client))
        assert result == ["TCS", "INFY"]

    def test_fetch_failure_yields_empty_list(self):
        client = _FakeAsyncClient(raises=[(cd._SOURCES["volume_shock"], RuntimeError("down"))])
        assert run(cd._fetch_volume_shock_universe(client)) == []


# ---------------------------------------------------------------------------
# _recently_candidated_symbols
# ---------------------------------------------------------------------------

class TestRecentlyCandidatedSymbols:
    def test_no_rows_returns_empty_set(self, db):
        assert cd._recently_candidated_symbols(db, "REAL") == set()

    def test_within_cooldown_window_included(self, db):
        db.add(models.TradeCandidate(
            mode="REAL", symbol="TCS",
            received_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ))
        db.commit()
        assert cd._recently_candidated_symbols(db, "REAL") == {"TCS"}

    def test_outside_cooldown_window_excluded(self, db):
        db.add(models.TradeCandidate(
            mode="REAL", symbol="OLD",
            received_at=datetime.now(timezone.utc) - timedelta(hours=48),
        ))
        db.commit()
        assert cd._recently_candidated_symbols(db, "REAL") == set()

    def test_other_mode_excluded(self, db):
        db.add(models.TradeCandidate(
            mode="DEMO", symbol="TCS",
            received_at=datetime.now(timezone.utc),
        ))
        db.commit()
        assert cd._recently_candidated_symbols(db, "REAL") == set()

    def test_custom_hours_window_respected(self, db):
        db.add(models.TradeCandidate(
            mode="REAL", symbol="RECENT",
            received_at=datetime.now(timezone.utc) - timedelta(hours=1.5),
        ))
        db.commit()
        # 1h window excludes a 1.5h-old row even though the 6h default wouldn't
        assert cd._recently_candidated_symbols(db, "REAL", hours=1.0) == set()
        assert cd._recently_candidated_symbols(db, "REAL", hours=2.0) == {"RECENT"}

    def test_gate6_skip_shrinks_cooldown_when_stale(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_GATE6_REQUEUE_MINUTES", 15)
        cand = models.TradeCandidate(
            mode="REAL", symbol="GATE6SKIP",
            received_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(cand)
        db.commit()
        db.add(models.TradeDecision(
            mode="REAL", symbol="GATE6SKIP", candidate_id=cand.id,
            decision_type="ENTRY", action="WAIT",
            reasoning="rejected by composite quality score ranking",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        ))
        db.commit()
        # decision is 30 min old, requeue window is 15 min -> stale -> re-queued
        assert cd._recently_candidated_symbols(db, "REAL") == set()

    def test_gate6_skip_still_excluded_when_fresh(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_GATE6_REQUEUE_MINUTES", 60)
        cand = models.TradeCandidate(
            mode="REAL", symbol="GATE6FRESH",
            received_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(cand)
        db.commit()
        db.add(models.TradeDecision(
            mode="REAL", symbol="GATE6FRESH", candidate_id=cand.id,
            decision_type="ENTRY", action="WAIT",
            reasoning="rejected by composite quality score ranking",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ))
        db.commit()
        # decision only 5 min old, requeue window is 60 min -> still fresh -> stays excluded
        assert cd._recently_candidated_symbols(db, "REAL") == {"GATE6FRESH"}

    def test_non_gate6_wait_reason_keeps_full_cooldown(self, db):
        cand = models.TradeCandidate(
            mode="REAL", symbol="RISKREJECT",
            received_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(cand)
        db.commit()
        db.add(models.TradeDecision(
            mode="REAL", symbol="RISKREJECT", candidate_id=cand.id,
            decision_type="ENTRY", action="WAIT",
            reasoning="risk check failed: capital_share_cap exceeded",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        ))
        db.commit()
        # not a Gate 6 skip (different reasoning) -> full cooldown still applies
        assert cd._recently_candidated_symbols(db, "REAL") == {"RISKREJECT"}

    def test_snapshot_read_exception_falls_back_to_empty_set(self, db, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(db, "query", _boom)
        assert cd._recently_candidated_symbols(db, "REAL") == set()
