"""
tests/test_candidates_analysis.py

100%-coverage-plan round 2 for candidate_engine/candidates.py, following
round 1 (tests/test_candidates_helpers.py — the self-contained pure
helpers, row-normalizers, fetch wrappers, quality gate, and dedupe
lookup). This round targets the two multi-call analysis functions that
sit on top of those pieces:

  - _multi_tf_analysis    — the standard track's 7-timeframe + quote gate
  - _volume_shock_analysis — the momentum-breakout track's gate

Both are exercised with a routing fake httpx.AsyncClient (_RoutedAsyncClient)
that dispatches on (url, params) rather than round 1's plain URL-substring
router, because _multi_tf_analysis fires 7 concurrent GETs to the exact
same /history/{symbol} URL distinguished only by the `period` query param.

Still deferred to a further round: _refresh_standard_candidates,
_refresh_volume_shock_candidates, and refresh_candidates — the top-level
cycle orchestrators that wrap these two functions with DB writes,
intraday-restricted-symbol lookups, bulk-quote prefetch, and
sector-peer-aware quality gating across a whole candidate batch.

Run from services/real-trade-service:
    python3 -m pytest tests/test_candidates_analysis.py -q \
        --cov=candidate_engine.candidates --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import config
from candidate_engine import candidates as cd

MDU = cd.MARKET_DATA_URL


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_adaptive_globals():
    """Same rationale as round 1's _reset_module_globals — these are
    process-local module state a couple of tests here monkeypatch
    directly (ATR cap tests)."""
    saved = cd._adaptive_max_atr_pct
    yield
    cd._adaptive_max_atr_pct = saved


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        return self._payload


class _RoutedAsyncClient:
    """Dispatches GET/POST by callback rather than plain URL substring, so
    tests can distinguish the 7 concurrent /history/{symbol} calls
    _multi_tf_analysis fires (same URL, different `period` param)."""

    def __init__(self, get_router=None, post_router=None):
        self._get_router = get_router or (lambda url, params: _FakeResponse(404))
        self._post_router = post_router or (lambda url, json: _FakeResponse(404))
        self.calls = []

    async def get(self, url, timeout=None, params=None):
        self.calls.append(("GET", url, params))
        return self._get_router(url, params)

    async def post(self, url, timeout=None, json=None):
        self.calls.append(("POST", url, json))
        return self._post_router(url, json)


def _c(close, open_=None, high=None, low=None, volume=1000.0):
    o = open_ if open_ is not None else close
    return {
        "open": o, "close": close,
        "high": high if high is not None else max(o, close) + 0.5,
        "low": low if low is not None else min(o, close) - 0.5,
        "volume": volume,
    }


def _rising(n, start=100.0, step=1.0, volume=1000.0):
    out, price = [], start
    for _ in range(n):
        out.append(_c(price + step, open_=price, volume=volume))
        price += step
    return out


# ---------------------------------------------------------------------------
# _multi_tf_analysis
# ---------------------------------------------------------------------------

# Period strings _multi_tf_analysis actually requests, keyed by its own
# internal tf label (see the `periods` dict in the function).
_PERIOD_OF = {"1d": "1d", "1w": "5d", "1m": "1mo", "3m": "3mo", "6m": "6mo", "1y": "1y", "2y": "2y"}


def _history_router(by_tf: dict, default=None):
    """Builds a get_router for /history/{symbol}?period=X&interval=Y —
    by_tf maps our tf label ("1d","1w",...) to a candle list; any period
    not present returns `default` (empty list by default)."""
    by_period = {_PERIOD_OF[tf]: candles for tf, candles in by_tf.items()}

    def _route(url, params):
        if "/history/" in url:
            period = (params or {}).get("period")
            return _FakeResponse(200, {"candles": by_period.get(period, default or [])})
        if "/quote/" in url:
            return _FakeResponse(200, {"price": 100.0})
        return _FakeResponse(404)
    return _route


def _happy_by_tf():
    """Baseline dataset where every _multi_tf_analysis check passes:
    - 1w/1m/3m/2y bullish (>0.5%, weight 1 each) -> weighted score 4.0 == MIN_BULLISH_TIMEFRAMES
    - 6m flat (not < -10%) -> passes the downtrend gate without counting toward the score
    - 1y gives a wide 52w range so price=100 sits well under the top-12% cutoff
    - 1m is short (<10 candles) -> ATR/volume/resistance checks (5/6/7) all no-op/pass
    """
    return {
        "1d": [_c(100), _c(100)],                       # flat, weight 0.5, doesn't matter
        "1w": [_c(100, open_=100), _c(105, open_=100)],  # +5%
        "1m": [_c(100, open_=100), _c(110, open_=100), _c(112, open_=100)],  # +12%, <10 candles
        "3m": [_c(100, open_=100), _c(108, open_=100)],  # +8%
        "6m": [_c(100, open_=100), _c(99, open_=100)],   # -1%, not a downtrend reject
        "1y": [
            _c(90, open_=90, high=120, low=50),
            _c(150, open_=90, high=200, low=80),
            _c(100, open_=150, high=180, low=100),
        ],
        "2y": [_c(100, open_=100), _c(106, open_=100)],  # +6%
    }


class TestMultiTfAnalysisHappyPath:
    def test_all_checks_pass(self):
        client = _RoutedAsyncClient(get_router=_history_router(_happy_by_tf()))
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert result["reject_reason"] is None
        assert result["current_price"] == 100.0
        # 1w/1m/3m/2y/1y all clear the bullish threshold (weight 1 each);
        # 1d is flat (doesn't count) -> 5.0, comfortably >= MIN_BULLISH_TIMEFRAMES
        assert result["bullish_count"] == 5.0
        assert result["atr_pct"] is None  # <15 1m candles -> ATR check skipped
        assert "market_note" in result


class TestMultiTfAnalysisDataStarved:
    def test_no_quote_and_no_history_is_data_starved(self):
        client = _RoutedAsyncClient(get_router=lambda url, params: _FakeResponse(404))
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert result["data_starved"] is True
        assert "data fetch failed for all timeframes" in result["reject_reason"]

    def test_no_quote_but_some_history_is_not_data_starved(self):
        # quote 404s, but 1w history resolves -> not "everything empty",
        # so this hits the plain "no live quote" branch instead.
        by_tf = {"1w": [_c(100, open_=100), _c(105, open_=100)]}
        client = _RoutedAsyncClient(get_router=_history_router(by_tf))
        # override quote to 404 specifically
        base_route = _history_router(by_tf)

        def _route(url, params):
            if "/quote/" in url:
                return _FakeResponse(404)
            return base_route(url, params)
        client = _RoutedAsyncClient(get_router=_route)
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert result.get("data_starved") is not True
        assert "No live quote available" in result["reject_reason"]


class TestMultiTfAnalysisChecks:
    def test_quote_resolves_but_zero_price_rejects(self):
        by_tf = _happy_by_tf()

        def _route(url, params):
            if "/quote/" in url:
                return _FakeResponse(200, {"price": 0})
            return _history_router(by_tf)(url, params)
        client = _RoutedAsyncClient(get_router=_route)
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert "No live quote available" in result["reject_reason"]

    def test_below_min_price_rejects(self):
        by_tf = _happy_by_tf()

        def _route(url, params):
            if "/quote/" in url:
                return _FakeResponse(200, {"price": 15.0})
            return _history_router(by_tf)(url, params)
        client = _RoutedAsyncClient(get_router=_route)
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert "below" in result["reject_reason"] and "minimum" in result["reject_reason"]

    def test_6m_downtrend_rejects(self):
        by_tf = _happy_by_tf()
        by_tf["6m"] = [_c(100, open_=100), _c(85, open_=100)]  # -15%
        client = _RoutedAsyncClient(get_router=_history_router(by_tf))
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert "6m return" in result["reject_reason"]

    def test_insufficient_weighted_bullish_score_rejects(self):
        # Every period comes back empty -> all tf_returns None -> score 0.
        client = _RoutedAsyncClient(get_router=_history_router({}))
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert "Weighted bullish score" in result["reject_reason"]
        assert result["bullish_count"] == 0.0

    def test_overextended_52w_rejects(self):
        by_tf = _happy_by_tf()
        # quote price is fixed at 100 by _history_router; 52w low=50, high=101
        # -> pos_pct = (100-50)/(101-50)*100 = 98% > 88% (top 12%) -> rejects
        by_tf["1y"] = [
            _c(96, open_=96, high=101, low=50),
            _c(100, open_=96, high=101, low=60),
        ]
        client = _RoutedAsyncClient(get_router=_history_router(by_tf))
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert "top" in result["reject_reason"] and "52w range" in result["reject_reason"]

    def test_atr_cap_exceeded_rejects(self, monkeypatch):
        by_tf = _happy_by_tf()
        by_tf["1m"] = _rising(20, start=100.0, step=1.0)  # >=15 candles -> ATR computable
        monkeypatch.setattr(cd, "_adaptive_max_atr_pct", 0.01)  # impossible to clear
        client = _RoutedAsyncClient(get_router=_history_router(by_tf))
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert "ATR" in result["reject_reason"] and "cap" in result["reject_reason"]
        assert result["atr_pct"] is not None

    def test_unhealthy_volume_rejects(self):
        by_tf = _happy_by_tf()
        candles = _rising(20, start=100.0, step=1.0, volume=1000.0)
        for c in candles[-5:]:
            c["volume"] = 1.0  # tank last 5 days
        by_tf["1m"] = candles
        client = _RoutedAsyncClient(get_router=_history_router(by_tf))
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert "volume" in result["reject_reason"].lower()

    def test_near_resistance_rejects(self):
        by_tf = _happy_by_tf()
        candles = _rising(20, start=100.0, step=1.0, volume=1000.0)
        by_tf["1m"] = candles
        recent_high = max(c["high"] for c in candles[-20:])

        def _route(url, params):
            if "/quote/" in url:
                return _FakeResponse(200, {"price": recent_high * 0.99})
            return _history_router(by_tf)(url, params)
        client = _RoutedAsyncClient(get_router=_route)
        result = run(cd._multi_tf_analysis(client, "TCS"))
        assert "resistance" in result["reject_reason"]


# ---------------------------------------------------------------------------
# _volume_shock_analysis
# ---------------------------------------------------------------------------

def _vs_router(quote=None, candles=None, quote_status=200, delivery=None, delivery_status=200):
    def _route(url, params):
        if "/quote/" in url:
            return _FakeResponse(quote_status, quote)
        if "/history/" in url:
            return _FakeResponse(200, {"candles": candles or []})
        if "/delivery/" in url:
            return _FakeResponse(delivery_status, delivery)
        return _FakeResponse(404)
    return _route


def _shock_candles(n=6, base=100.0, today_return_pct=5.0, volume=1000.0, today_volume=None):
    """n daily candles, flat except the last (today), which jumps by
    today_return_pct off the prior close. Volume flat except today's,
    which defaults to a genuine shock multiple of the prior average."""
    candles = [_c(base, open_=base, volume=volume) for _ in range(n - 1)]
    today_close = round(base * (1 + today_return_pct / 100), 2)
    tv = today_volume if today_volume is not None else volume * 3
    candles.append(_c(today_close, open_=base, volume=tv))
    return candles


class TestVolumeShockAnalysis:
    def test_no_quote_rejects(self):
        client = _RoutedAsyncClient(get_router=_vs_router(quote=None, quote_status=404))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "No quote available" in result["reject_reason"]

    def test_insufficient_history_rejects(self):
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 100.0}, candles=_shock_candles(4),
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "Insufficient daily history" in result["reject_reason"]

    def test_quote_with_no_usable_price_rejects(self):
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"other": 1}, candles=_shock_candles(),
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "no usable price" in result["reject_reason"]

    def test_below_min_price_rejects(self):
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 15.0}, candles=_shock_candles(base=15.0),
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "below" in result["reject_reason"] and "minimum" in result["reject_reason"]

    def test_cannot_compute_return_rejects(self):
        candles = _shock_candles()
        candles[-2]["close"] = 0  # prior_close <= 0
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 100.0}, candles=candles,
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "Could not compute today's return" in result["reject_reason"]

    def test_below_min_return_threshold_rejects(self):
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 101.0}, candles=_shock_candles(today_return_pct=1.0),
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "volume-shock breakout threshold" in result["reject_reason"]

    def test_insufficient_volume_history_rejects(self):
        candles = _shock_candles(today_return_pct=5.0)
        for c in candles[:-1]:
            c["volume"] = 0  # drop below 6 valid volume readings
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 105.0}, candles=candles,
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "Insufficient volume history" in result["reject_reason"]

    def test_below_volume_multiple_threshold_rejects(self):
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 105.0},
            candles=_shock_candles(today_return_pct=5.0, volume=1000.0, today_volume=1000.0),  # 1.0x
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "volume-shock threshold" in result["reject_reason"]

    def test_atr_cap_exceeded_rejects(self, monkeypatch):
        # 16 daily candles so candles[:-1] has 15 -> ATR computable
        candles = _rising(15, start=100.0, step=2.0, volume=1000.0)
        today_close = round(candles[-1]["close"] * 1.05, 2)
        candles.append(_c(today_close, open_=candles[-1]["close"], volume=5000.0))
        monkeypatch.setattr(cd, "_adaptive_max_atr_pct", 0.01)
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": today_close}, candles=candles,
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "ATR" in result["reject_reason"]
        assert result["atr_pct"] is not None

    def test_base_tier_low_delivery_rejects(self):
        candles = _shock_candles(today_return_pct=5.0, volume=1000.0, today_volume=3000.0)
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 105.0}, candles=candles,
            delivery={"delivery_pct": 10.0, "source": "nse"},
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert "Delivery" in result["reject_reason"]

    def test_base_tier_missing_delivery_data_does_not_reject(self):
        candles = _shock_candles(today_return_pct=5.0, volume=1000.0, today_volume=3000.0)
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 105.0}, candles=candles,
            delivery={"delivery_pct": 50.0, "source": "fallback_neutral"},
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert result["reject_reason"] is None
        assert result["delivery_pct"] is None
        assert result["high_delivery"] is None

    def test_base_tier_passes_with_high_delivery(self):
        candles = _shock_candles(today_return_pct=5.0, volume=1000.0, today_volume=3000.0)
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 105.0}, candles=candles,
            delivery={"delivery_pct": 65.0, "source": "nse"},
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert result["reject_reason"] is None
        assert result["delivery_pct"] == 65.0
        assert result["high_delivery"] is True
        assert result["high_conviction"] is False
        assert result["upper_circuit"] is False
        assert "vol_shock" in result["backtest_note"]

    def test_high_conviction_skips_delivery_fetch(self):
        candles = _shock_candles(today_return_pct=16.0, volume=1000.0, today_volume=20000.0)  # 20x
        router = _vs_router(quote={"price": 116.0}, candles=candles,
                             delivery={"delivery_pct": 5.0, "source": "nse"})
        client = _RoutedAsyncClient(get_router=router)
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert result["reject_reason"] is None
        assert result["high_conviction"] is True
        assert result["upper_circuit"] is False
        assert not any("/delivery/" in c[1] for c in client.calls)
        assert "high_conviction" in result["backtest_note"]

    def test_upper_circuit_classification(self):
        candles = _shock_candles(today_return_pct=20.0, volume=1000.0, today_volume=5000.0)
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 120.0}, candles=candles,
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        assert result["reject_reason"] is None
        assert result["upper_circuit"] is True
        assert result["high_conviction"] is True
        assert "upper_circuit" in result["backtest_note"]

    def test_full_happy_path_field_shape(self):
        candles = _shock_candles(today_return_pct=5.0, volume=1000.0, today_volume=3000.0)
        client = _RoutedAsyncClient(get_router=_vs_router(
            quote={"price": 105.0}, candles=candles,
            delivery={"delivery_pct": 65.0, "source": "nse"},
        ))
        result = run(cd._volume_shock_analysis(client, "TCS"))
        for key in (
            "reject_reason", "today_return_pct", "vol_multiple", "atr_pct",
            "current_price", "high_conviction", "upper_circuit", "delivery_pct",
            "high_delivery", "time_stop_hint", "backtest_note",
        ):
            assert key in result
        assert result["time_stop_hint"] == "EOD+1"
