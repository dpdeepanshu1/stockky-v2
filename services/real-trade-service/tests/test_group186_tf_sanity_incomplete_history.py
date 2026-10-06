"""
group186 (items 1 and 2 of the 2026-10-06 boot-log list): candidate timeframe returns.

  * HFCL scored 1w=+211.28% next to 1m=+4.72%: a bad first bar in the 5d history. A short horizon above its cap
    AND far above the next longer horizon is dropped (None) and logged; with no longer horizon to compare against
    it is kept, and CANDIDATE_TF_SANITY=0 turns the check off.
  * TCS came back with 1d/1w/6m/1y/2y all None (history fetches failed) and was rejected as "weighted bullish
    score 1.0". When the missing horizons could still lift the score to the threshold the reason is now
    "Incomplete history" (data_incomplete=True); when they could not, it stays a weak-momentum rejection.
No network: fake client, same shape as tests/test_candidates_analysis.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import config  # noqa: F401
from candidate_engine import candidates as cd


def run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, code=200, payload=None):
        self.status_code, self._p, self.text = code, payload, ""

    def json(self):
        return self._p


class _Client:
    def __init__(self, by_period, price=100.0):
        self.by_period, self.price = by_period, price

    async def get(self, url, timeout=None, params=None):
        if "/history/" in url:
            return _Resp(200, {"candles": self.by_period.get((params or {}).get("period"), [])})
        if "/quote/" in url:
            return _Resp(200, {"price": self.price})
        return _Resp(404)

    async def post(self, url, timeout=None, json=None):
        return _Resp(404)


def _c(close, open_=None):
    o = open_ if open_ is not None else close
    return {"open": o, "close": close, "high": max(o, close) + 0.5, "low": min(o, close) - 0.5, "volume": 1000.0}


_PERIOD = {"1d": "1d", "1w": "5d", "1m": "1mo", "3m": "3mo", "6m": "6mo", "1y": "1y", "2y": "2y"}


def _by_period(pct: dict):
    """tf -> return pct (None = history missing). 1y gets a wide range so the 52w check passes."""
    out = {}
    for tf, p in pct.items():
        if p is None:
            continue
        out[_PERIOD[tf]] = [_c(100, open_=100), _c(100 * (1 + p / 100), open_=100)]
    if "1y" in pct and pct["1y"] is not None:
        out["1y"] = [
            {"open": 90, "close": 90, "high": 120, "low": 50, "volume": 1000.0},
            {"open": 90, "close": 150, "high": 200, "low": 80, "volume": 1000.0},
            {"open": 150, "close": 100, "high": 180, "low": 100, "volume": 1000.0},
        ]
    return out


# ── _sanitize_tf_returns ─────────────────────────────────────────────────────

class TestSanitize:
    def test_hfcl_case_weekly_211_vs_month_4_is_dropped(self):
        clean, dropped = cd._sanitize_tf_returns({"1d": None, "1w": 211.28, "1m": 4.72, "3m": 17.22})
        assert clean["1w"] is None and dropped == ["1w"]
        assert clean["1m"] == 4.72 and clean["3m"] == 17.22

    def test_real_big_week_confirmed_by_the_month_is_kept(self):
        clean, dropped = cd._sanitize_tf_returns({"1w": 80.0, "1m": 95.0})
        assert clean["1w"] == 80.0 and dropped == []

    def test_no_longer_horizon_to_compare_keeps_the_value(self):
        clean, dropped = cd._sanitize_tf_returns({"1w": 211.0, "1m": None})
        assert clean["1w"] == 211.0 and dropped == []

    def test_below_cap_is_never_dropped(self):
        clean, dropped = cd._sanitize_tf_returns({"1d": 20.0, "1w": 0.1, "1m": 0.0})
        assert dropped == []

    def test_one_day_spike_against_a_flat_week_is_dropped(self):
        clean, dropped = cd._sanitize_tf_returns({"1d": 90.0, "1w": 2.0})
        assert clean["1d"] is None and dropped == ["1d"]

    def test_input_is_not_mutated(self):
        src = {"1w": 211.28, "1m": 4.72}
        cd._sanitize_tf_returns(src)
        assert src["1w"] == 211.28

    def test_switch_off(self, monkeypatch):
        monkeypatch.setattr(cd, "TF_SANITY_ENABLED", False)
        clean, dropped = cd._sanitize_tf_returns({"1w": 211.28, "1m": 4.72})
        assert clean["1w"] == 211.28 and dropped == []

    def test_negative_spike_is_judged_by_magnitude(self):
        clean, dropped = cd._sanitize_tf_returns({"1w": -75.0, "1m": -2.0})
        assert dropped == ["1w"]


class TestMissingWeight:
    def test_weights_of_none_horizons(self):
        r = {"1d": None, "1w": None, "1m": -7.67, "3m": 2.77, "6m": None, "1y": None, "2y": None}
        assert cd._missing_horizon_weight(r) == 0.5 + 1.0 + 1.0 + 1.0 + 1.0

    def test_nothing_missing(self):
        assert cd._missing_horizon_weight({"1d": 1.0, "1w": -1.0}) == 0.0


# ── _multi_tf_analysis ───────────────────────────────────────────────────────

class TestMultiTf:
    def test_hfcl_bogus_week_no_longer_counts_as_bullish(self, caplog):
        # 1m +4.72, 3m +17.22 bullish; 1w +211 bogus. Only 1m and 3m count -> 2.0 < 4. The 1w is dropped, not scored.
        by = _by_period({"1d": None, "1w": 211.28, "1m": 4.72, "3m": 17.22,
                         "6m": 1.0, "1y": 1.0, "2y": 1.0})
        with caplog.at_level(logging.WARNING):
            res = run(cd._multi_tf_analysis(_Client(by), "HFCL"))
        assert res["tf_returns"]["1w"] is None
        assert "dropped as implausible" in caplog.text and "HFCL" in caplog.text

    def test_bogus_week_cannot_carry_a_candidate_over_the_line(self):
        # without the guard: 1w(211) + 1m + 3m + 2y = 4.0 passes. With it: 3.0 and the 1w shows as missing.
        by = _by_period({"1d": None, "1w": 211.28, "1m": 4.72, "3m": 17.22,
                         "6m": 0.0, "1y": None, "2y": 6.0})
        res = run(cd._multi_tf_analysis(_Client(by), "HFCL"))
        assert res["reject_reason"] is not None
        assert res["bullish_count"] == 3.0

    def test_tcs_missing_horizons_is_incomplete_not_weak(self):
        by = _by_period({"1d": None, "1w": None, "1m": -7.67, "3m": 2.77, "6m": None, "1y": None, "2y": None})
        res = run(cd._multi_tf_analysis(_Client(by, price=3000.0), "TCS"))
        assert res["data_incomplete"] is True
        assert res["reject_reason"].startswith("Incomplete history")
        assert "1d, 1w, 6m, 1y, 2y" in res["reject_reason"]
        assert "Weighted bullish score" not in res["reject_reason"]

    def test_all_history_empty_is_incomplete(self):
        res = run(cd._multi_tf_analysis(_Client({}), "TCS"))
        assert res["data_incomplete"] is True

    def test_missing_horizons_that_cannot_reach_the_threshold_stay_weak_momentum(self):
        # five horizons bearish/flat, only 1d and 2y missing: max 0 + 0.5 + 1.0 = 1.5 < 4
        by = _by_period({"1d": None, "1w": -1.0, "1m": -2.0, "3m": -1.0, "6m": -1.0, "1y": -1.0, "2y": None})
        res = run(cd._multi_tf_analysis(_Client(by), "WEAK"))
        assert "Weighted bullish score" in res["reject_reason"]
        assert not res.get("data_incomplete")

    def test_complete_history_unchanged_pass(self):
        by = _by_period({"1d": 0.0, "1w": 5.0, "1m": 12.0, "3m": 8.0, "6m": -1.0, "1y": 0.0, "2y": 6.0})
        res = run(cd._multi_tf_analysis(_Client(by), "GOOD"))
        assert res["reject_reason"] is None
