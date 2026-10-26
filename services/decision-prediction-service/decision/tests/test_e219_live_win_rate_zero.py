"""E219: a live win rate of 0 was read as "missing" (closed-loop bar-raise lost exactly when the live edge is worst).

`(training).get("live_win_rate") or (training).get("win_rate")` (decision/main.py, twice) and
`st.get("live_win_rate") or st.get("win_rate") or st.get("overall_win_rate")` (training/app.py `/training-score`) all treat 0.0 as absent.
0.0 means every evaluated outcome lost. `_live_win_rate_threshold_shift` / `_threshold_offsets` turn that into the strongest "raise the
bar" shift (+8), but the value never reached them: the training service answered `null` and the decision service chain gave `None`.
First value that is present (not None) now wins, in all three places.

Run from services/decision-prediction-service/decision:
    python3 -m pytest tests/test_e219_live_win_rate_zero.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Not skip_http: the training pillar is read from get_training_score (patched below), not from the payload.
LOW = {"technical_score": 30, "fundamental_score": 30, "news_score": 30, "close": 100.0, "rsi": 40, "market_score": 50}


@pytest.fixture()
def m(monkeypatch):
    spec = importlib.util.spec_from_file_location("decision_main_e219", os.path.join(_ROOT, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._decide_mem_cache.clear()
    monkeypatch.setattr(mod, "_redis", None)
    monkeypatch.setattr(mod, "_kv", None)
    monkeypatch.setattr(mod, "_get_http_client", lambda: object())

    async def _no_fetch(client, url, label):
        return None

    monkeypatch.setattr(mod, "_fetch_optional", _no_fetch)
    return mod


def _run(mod, monkeypatch, training):
    """Run _decide_impl with a given training payload; return (live_win_rate given to _decide, flags given to multi_horizon_decide)."""
    seen = {}

    async def _training(symbol):
        return dict(training)

    monkeypatch.setattr(mod, "get_training_score", _training)

    real_decide = mod._decide

    def _spy_decide(*a, **kw):
        seen["decide_wr"] = kw.get("live_win_rate")
        seen["decide_n"] = kw.get("live_win_rate_n")
        return real_decide(*a, **kw)

    monkeypatch.setattr(mod, "_decide", _spy_decide)

    real_mh = mod.multi_horizon_decide

    def _spy_mh(**kw):
        seen["flags_wr"] = (kw.get("flags") or {}).get("live_win_rate")
        return real_mh(**kw)

    monkeypatch.setattr(mod, "multi_horizon_decide", _spy_mh)

    asyncio.run(mod._decide_impl("LOWCO", already_owned=False, force=True, background_tasks=None, prefetched=dict(LOW)))
    return seen


class TestDecisionServiceChain:
    def test_zero_live_win_rate_is_kept(self, m, monkeypatch):
        seen = _run(m, monkeypatch, {"training_score": 50, "live_win_rate": 0.0, "live_win_rate_n": 30})
        assert seen["decide_wr"] == 0.0 and seen["decide_wr"] is not None
        assert seen["flags_wr"] == 0.0 and seen["flags_wr"] is not None

    def test_zero_live_win_rate_raises_the_bar(self, m):
        # The point of keeping it: 0 wins in 30 outcomes is the strongest bar-raise; None is no shift at all.
        assert m._live_win_rate_threshold_shift(0.0, 30) == 8.0
        assert m._live_win_rate_threshold_shift(None, 30) == 0.0

    def test_win_rate_key_used_when_live_win_rate_missing(self, m, monkeypatch):
        seen = _run(m, monkeypatch, {"training_score": 50, "win_rate": 0.4, "live_win_rate_n": 30})
        assert seen["decide_wr"] == 0.4 and seen["flags_wr"] == 0.4

    def test_live_win_rate_wins_over_win_rate_when_both_present(self, m, monkeypatch):
        seen = _run(m, monkeypatch, {"training_score": 50, "live_win_rate": 0.6, "win_rate": 0.3, "live_win_rate_n": 30})
        assert seen["decide_wr"] == 0.6 and seen["flags_wr"] == 0.6

    def test_both_zero_stays_zero(self, m, monkeypatch):
        seen = _run(m, monkeypatch, {"training_score": 50, "live_win_rate": 0.0, "win_rate": 0.0, "live_win_rate_n": 30})
        assert seen["decide_wr"] == 0.0

    def test_both_missing_uses_t1_probability_as_before(self, m, monkeypatch):
        seen = _run(m, monkeypatch, {"training_score": 50, "t1_success_probability": 70.0, "live_win_rate_n": 30})
        assert seen["decide_wr"] == 70.0       # unchanged behaviour
        assert seen["flags_wr"] is None        # the flags value never used the t1 fallback

    def test_nothing_present_stays_none(self, m, monkeypatch):
        seen = _run(m, monkeypatch, {"training_score": 50, "live_win_rate_n": 0})
        assert seen["decide_wr"] is None and seen["flags_wr"] is None


class TestTrainingServiceChain:
    """training/app.py needs sqlalchemy etc., so the real source line is pulled out and evaluated (not a copy of it)."""

    @staticmethod
    def _pick(st):
        src = (Path(_ROOT).parent / "training" / "app.py").read_text(encoding="utf-8")
        mm = re.search(r"^\s*live_wr = next\(.*\)\s*$", src, flags=re.M)
        assert mm, "training/app.py: the first-present live_wr line is gone"
        scope = {"st": st}
        exec(mm.group(0).strip(), {}, scope)
        return scope["live_wr"]

    def test_no_or_chain_left(self):
        src = (Path(_ROOT).parent / "training" / "app.py").read_text(encoding="utf-8")
        assert 'st.get("live_win_rate") or' not in src

    def test_zero_global_rate_is_returned(self):
        assert self._pick({"live_win_rate": 0.0, "win_rate": 0.0}) == 0.0

    def test_zero_live_win_rate_with_no_win_rate_is_returned(self):
        assert self._pick({"live_win_rate": 0.0}) == 0.0

    def test_alternate_keys_still_used_in_order(self):
        assert self._pick({"win_rate": 0.4}) == 0.4
        assert self._pick({"overall_win_rate": 0.7}) == 0.7
        assert self._pick({"live_win_rate": 0.5, "win_rate": 0.4, "overall_win_rate": 0.7}) == 0.5

    def test_nothing_present_is_none(self):
        assert self._pick({}) is None
        assert self._pick({"live_win_rate": None, "win_rate": None}) is None
