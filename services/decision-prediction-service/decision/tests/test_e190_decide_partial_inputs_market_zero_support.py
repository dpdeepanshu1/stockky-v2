"""E190: three defects in `_decide_impl` left open by E189.

1. A decision computed from a `skip_http` payload (events / prediction / training pillars skipped) is cached under the same key as a full one and
   could be served to a later full request. It now carries `partial_inputs` and is served only to another skip_http request (and the api-gateway
   single-stock fast path skips it).
2. `prefetched.get("market_score") or prefetched.get("sentiment_score")` read a real market_score of 0 as missing. First present value now wins.
3. The legacy entry levels used the support even when it was at or above the close (entry_low above entry_high, stop above the price). Such a
   support is now treated like a missing one (close * 0.95).

Run from services/decision-prediction-service/decision:
    python3 -m pytest tests/test_e190_decide_partial_inputs_market_zero_support.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

import pytest

pytest.importorskip("fastapi")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

LOW = {"skip_http": True, "technical_score": 30, "fundamental_score": 30, "news_score": 30, "close": 100.0, "rsi": 40, "market_score": 50}


@pytest.fixture()
def m(monkeypatch):
    spec = importlib.util.spec_from_file_location("decision_main_e190", os.path.join(_ROOT, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._decide_mem_cache.clear()
    monkeypatch.setattr(mod, "_redis", None)
    monkeypatch.setattr(mod, "_kv", None)
    monkeypatch.setattr(mod, "_get_http_client", lambda: object())
    return mod


def _run(mod, owned=False, force=False, pre=None):
    return asyncio.run(mod._decide_impl("LOWCO", already_owned=owned, force=force, background_tasks=None, prefetched=dict(pre or LOW)))


class TestPartialInputs:
    def test_truth_table(self, m):
        f = m._decide_cache_matches
        assert f({"already_owned": False, "partial_inputs": True}, False, True) is True
        assert f({"already_owned": False, "partial_inputs": True}, False, False) is False
        assert f({"already_owned": False, "partial_inputs": False}, False, False) is True
        assert f({"already_owned": False, "partial_inputs": False}, False, True) is True
        assert f({"already_owned": False}, False, False) is True            # entry from before E190: no marker, counts as full
        assert f({"already_owned": True, "partial_inputs": True}, False, True) is False   # E189 mode check still applies

    def test_payload_carries_the_marker(self, m):
        assert _run(m, force=True)["partial_inputs"] is True

    def test_a_skip_http_entry_is_served_to_a_skip_http_request(self, m):
        first = _run(m, force=True)
        again = _run(m)
        assert first["from_cache"] is False and again["from_cache"] is True

    def test_a_skip_http_entry_is_not_served_to_a_full_request(self, m):
        _run(m, force=True)
        full = dict(LOW)
        full.pop("skip_http")
        out = _run(m, pre=full)          # full request: must recompute (here its HTTP stage fails -> error fallback), never the cached partial
        assert out.get("from_cache") is not True
        assert out.get("partial_inputs") is not True or out.get("from_cache") is False

    def test_gateway_fast_path_skips_a_partial_entry(self):
        from pathlib import Path
        src = (Path(_ROOT).parents[1] / "api-gateway" / "main.py").read_text(encoding="utf-8")
        i = src.index("cached_decide = _redis_get(cache_key)")
        assert 'cached_decide.get("partial_inputs")' in src[i:i + 1600]   # E191 relaxed this: see test_e191_gateway_partial_cache_and_sentiment_zero.py


class TestMarketScoreZero:
    def test_zero_market_score_is_used(self, m):
        pre = dict(LOW, market_score=0)
        assert _run(m, force=True, pre=pre)["market_score"] == 0

    def test_zero_market_score_beats_sentiment_score(self, m):
        pre = dict(LOW, market_score=0, sentiment_score=80)
        assert _run(m, force=True, pre=pre)["market_score"] == 0

    def test_sentiment_score_used_when_market_score_absent(self, m):
        pre = {k: v for k, v in LOW.items() if k != "market_score"}
        pre["sentiment_score"] = 64
        assert _run(m, force=True, pre=pre)["market_score"] == 64

    def test_neither_present_is_neutral_50(self, m):
        pre = {k: v for k, v in LOW.items() if k != "market_score"}
        assert _run(m, force=True, pre=pre)["market_score"] == 50

    def test_ordinary_market_score_unchanged(self, m):
        assert _run(m, force=True, pre=dict(LOW, market_score=72))["market_score"] == 72


class TestSupportAboveClose:
    def test_support_below_close_is_used(self, m):
        out = _run(m, force=True, pre=dict(LOW, support=90.0))
        assert out["entry_range"]["low"] == round(90.0 * 1.01, 2)
        assert out["stop_loss"] == round(90.0 * 0.98, 2)

    @pytest.mark.parametrize("support", [100.0, 104.0, 150.0])
    def test_support_at_or_above_close_falls_back(self, m, support):
        out = _run(m, force=True, pre=dict(LOW, support=support))
        assert out["entry_range"]["low"] == round(95.0 * 1.01, 2)
        assert out["stop_loss"] == round(95.0 * 0.98, 2)
        assert out["entry_range"]["low"] <= out["entry_range"]["high"]
        assert out["stop_loss"] < 100.0

    def test_missing_support_unchanged(self, m):
        out = _run(m, force=True)
        assert out["entry_range"]["low"] == round(95.0 * 1.01, 2)
        assert out["stop_loss"] == round(95.0 * 0.98, 2)
