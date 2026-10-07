"""tests/test_group221_indices_prev_close.py — /market/indices reports the real previous-session change (group 221).

`history(period="1d")` is one row, so the endpoint's `len(hist) > 1` previous-close branch never runs and
`nifty.change_pct` is "vs today's open". That is deliberately left alone (the regime score and the dashboard
are tuned on it). `_prev_session_change` reads the real previous close separately and the payload carries it
as `nifty_vs_prev_close` / `sensex_vs_prev_close` for the scalp market gate.

Run from services/api-gateway:
    python3 -m pytest tests/test_group221_indices_prev_close.py -v
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pandas as pd
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


class KV:
    def __init__(self):
        self.store = {}
        self.sets = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.sets.append((key, value, ttl))
        self.store[key] = value


@pytest.fixture
def kv(monkeypatch):
    k = KV()
    monkeypatch.setattr(gw, "_redis_get", k.get)
    monkeypatch.setattr(gw, "_redis_set", k.set)
    return k


def _dated(closes, last_day_offset=0):
    """Frame of closes on consecutive IST calendar days ending `last_day_offset` days before today."""
    end = pd.Timestamp(datetime.now(gw.IST).date()) - pd.Timedelta(days=last_day_offset)
    idx = pd.date_range(end=end, periods=len(closes), freq="D", tz="Asia/Kolkata")
    return pd.DataFrame({"Open": closes, "Close": closes}, index=idx)


class FakeTicker:
    """history(period) -> frame registered for that period, or the raise registered for it."""

    def __init__(self, by_period):
        self.by_period = by_period
        self.calls = []

    def history(self, period=None):
        self.calls.append(period)
        out = self.by_period[period]
        if isinstance(out, Exception):
            raise out
        return out


# ── _prev_session_change ─────────────────────────────────────────────────────
class TestPrevSessionChange:
    def test_market_hours_uses_the_second_last_row(self):
        t = FakeTicker({"5d": _dated([101.0, 102.0, 100.0, 99.0])})   # last row is today's partial bar
        out = gw._prev_session_change(t, 99.0)
        assert out == {"prev_close": 100.0, "change_pct": -1.0}
        assert t.calls == ["5d"]

    def test_gap_down_flat_since_open_is_negative_vs_prev_close(self):
        t = FakeTicker({"5d": _dated([100.0, 98.8])})                 # gapped to ~98.8 and stayed there
        assert gw._prev_session_change(t, 98.8) == {"prev_close": 100.0, "change_pct": -1.2}

    def test_last_row_before_today_is_the_previous_close_itself(self):
        # pre-open, weekend or holiday: the last row is a finished session, so it IS the previous close
        t = FakeTicker({"5d": _dated([99.0, 100.0], last_day_offset=3)})
        assert gw._prev_session_change(t, 100.0) == {"prev_close": 100.0, "change_pct": 0.0}

    def test_plain_integer_index_falls_back_to_second_last_row(self):
        t = FakeTicker({"5d": pd.DataFrame({"Close": [100.0, 103.0]})})
        assert gw._prev_session_change(t, 103.0) == {"prev_close": 100.0, "change_pct": 3.0}

    def test_nan_rows_are_dropped_before_picking(self):
        df = pd.DataFrame({"Close": [100.0, float("nan"), 101.0]})
        assert gw._prev_session_change(FakeTicker({"5d": df}), 101.0) == {"prev_close": 100.0, "change_pct": 1.0}

    @pytest.mark.parametrize("df", [
        pd.DataFrame({"Close": [100.0]}),                 # one row: no previous session
        pd.DataFrame({"Close": []}),                      # empty
        pd.DataFrame({"Close": [float("nan"), 100.0]}),   # only one usable row
        pd.DataFrame({"Open": [1.0, 2.0]}),               # no Close column
    ])
    def test_unusable_history_gives_none(self, df):
        assert gw._prev_session_change(FakeTicker({"5d": df}), 100.0) is None

    @pytest.mark.parametrize("prev,last", [(0.0, 100.0), (-5.0, 100.0), (100.0, 0.0),
                                           (100.0, float("nan")), (100.0, float("inf")), (100.0, "abc"),
                                           (100.0, None)])
    def test_bad_numbers_give_none(self, prev, last):
        df = pd.DataFrame({"Close": [prev, 100.0]})
        # prev sits in row -2; the last row only needs to exist
        assert gw._prev_session_change(FakeTicker({"5d": df}), last) is None

    def test_a_failing_history_call_is_swallowed(self):
        assert gw._prev_session_change(FakeTicker({"5d": RuntimeError("yahoo down")}), 100.0) is None


# ── /market/indices ──────────────────────────────────────────────────────────
@pytest.fixture
def idx(monkeypatch, kv):
    """Production-shaped Ticker: period 1d is ONE row (open-based), period 5d has the previous session."""
    state = {"tickers": {}}

    def build(nifty_1d, nifty_5d, sensex_1d, sensex_5d):
        state["tickers"] = {
            "^NSEI": FakeTicker({"1d": nifty_1d, "5d": nifty_5d}),
            "^BSESN": FakeTicker({"1d": sensex_1d, "5d": sensex_5d}),
        }
        monkeypatch.setattr(gw.yf, "Ticker", lambda name: state["tickers"][name])
        return state["tickers"]

    return build


def _one_row(open_, close):
    return pd.DataFrame({"Open": [open_], "Close": [close]})


class TestIndicesPayload:
    def test_open_based_fields_are_unchanged_and_prev_close_block_is_added(self, idx, kv):
        # today: opened 98, trades 99, previous close 100  ->  +1.02% vs open, -1.0% vs previous close
        idx(_one_row(98.0, 99.0), _dated([101.0, 100.0, 99.0]),
            _one_row(98.0, 99.0), _dated([101.0, 100.0, 99.0]))
        body = json.loads(gw.get_market_indices().body)
        assert body["nifty"] == {"price": 99.0, "change": 1.0, "change_pct": 1.02}
        assert body["sensex"] == {"price": 99.0, "change": 1.0, "change_pct": 1.02}
        assert body["nifty_vs_prev_close"] == {"prev_close": 100.0, "change_pct": -1.0}
        assert body["sensex_vs_prev_close"] == {"prev_close": 100.0, "change_pct": -1.0}
        # the regime score / mood still follow the open-based change, exactly as before group 221
        assert body["market_score"] == 84 and body["market_mood"] == "BULLISH"

    def test_block_is_cached_and_remembered_with_the_rest_of_the_payload(self, idx, kv):
        idx(_one_row(98.0, 99.0), _dated([100.0, 99.0]), _one_row(98.0, 99.0), _dated([100.0, 99.0]))
        body = json.loads(gw.get_market_indices().body)
        assert (gw.INDICES_CACHE_KEY, body, 300) in kv.sets
        assert (gw.INDICES_LAST_KNOWN, body, 86400) in kv.sets

    def test_history_is_read_once_per_period_per_index(self, idx, kv):
        t = idx(_one_row(98.0, 99.0), _dated([100.0, 99.0]), _one_row(98.0, 99.0), _dated([100.0, 99.0]))
        gw.get_market_indices()
        assert t["^NSEI"].calls == ["1d", "5d"] and t["^BSESN"].calls == ["1d", "5d"]

    def test_a_failing_five_day_read_only_drops_the_block(self, idx, kv):
        idx(_one_row(98.0, 99.0), RuntimeError("yahoo 5d down"), _one_row(98.0, 99.0), _dated([100.0, 99.0]))
        body = json.loads(gw.get_market_indices().body)
        assert "nifty_vs_prev_close" not in body                       # that index lost only the new block
        assert body["sensex_vs_prev_close"] == {"prev_close": 100.0, "change_pct": -1.0}
        assert body["nifty"]["change_pct"] == 1.02 and "stale" not in body and "fallback" not in body

    def test_one_row_five_day_history_leaves_the_block_out(self, idx, kv):
        idx(_one_row(98.0, 99.0), _one_row(98.0, 99.0), _one_row(98.0, 99.0), _one_row(98.0, 99.0))
        body = json.loads(gw.get_market_indices().body)
        assert "nifty_vs_prev_close" not in body and "sensex_vs_prev_close" not in body
        assert body["nifty"]["change_pct"] == 1.02

    def test_zero_fallback_payload_has_no_block(self, idx, kv):
        idx(pd.DataFrame({"Open": [], "Close": []}), pd.DataFrame({"Close": []}),
            pd.DataFrame({"Open": [], "Close": []}), pd.DataFrame({"Close": []}))
        body = json.loads(gw.get_market_indices().body)
        assert body["fallback"] is True and "nifty_vs_prev_close" not in body
