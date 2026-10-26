"""E189: two defects in `_decide_impl`.

1. The decide cache is keyed by symbol alone, but `already_owned` changes the answer (owned: SELL below 35, HOLD 35-60, no data-quality gate; not
   owned: BUY NOW / PREPARE TO BUY / DO NOT BUY). A decision computed for a HELD stock was served to a candidate scan of the same symbol (and the
   reverse) for DECIDE_CACHE_TTL_OPEN seconds (12 h when the market is closed). The payload now carries `already_owned` and a cached entry is only
   served in the same mode; an entry written before this change has no marker and counts as "not owned".
2. The training record is made with the FINAL decision, but its arguments were only built when the LEGACY scorer had said BUY NOW / PREPARE TO BUY,
   so a final BUY that came from the multi-horizon scorer (the primary decision) was never recorded and the closed loop never saw it.
   DECIDE_RECORD_ON_FINAL_DECISION=0 restores the old gate.

Run from services/decision-prediction-service/decision:
    python3 -m pytest tests/test_e189_decide_cache_owned_and_final_record.py -v
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


class _BG:
    def __init__(self):
        self.calls = []

    def add_task(self, fn, *a, **k):
        self.calls.append((fn, a, k))


@pytest.fixture()
def m(monkeypatch):
    spec = importlib.util.spec_from_file_location("decision_main_e189", os.path.join(_ROOT, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._decide_mem_cache.clear()
    monkeypatch.setattr(mod, "_redis", None)
    monkeypatch.setattr(mod, "_kv", None)
    monkeypatch.setattr(mod, "_get_http_client", lambda: object())   # skip_http payloads never use it
    monkeypatch.delenv("DECIDE_RECORD_ON_FINAL_DECISION", raising=False)
    return mod


def _run(mod, owned=False, force=False, bg=None, pre=None):
    return asyncio.run(mod._decide_impl("LOWCO", already_owned=owned, force=force, background_tasks=bg, prefetched=dict(pre or LOW)))


class TestCacheMatches:
    def test_truth_table(self, m):
        f = m._decide_cache_matches
        assert f({"already_owned": True}, True) is True
        assert f({"already_owned": False}, False) is True
        assert f({"already_owned": True}, False) is False
        assert f({"already_owned": False}, True) is False

    def test_an_entry_without_a_marker_counts_as_not_owned(self, m):
        f = m._decide_cache_matches
        assert f({"decision": "BUY NOW"}, False) is True
        assert f({"decision": "BUY NOW"}, True) is False

    def test_not_a_dict(self, m):
        assert m._decide_cache_matches(None, False) is False
        assert m._decide_cache_matches("x", False) is False


class TestDecideCacheByMode:
    def test_payload_carries_the_mode(self, m):
        assert _run(m, owned=True, force=True)["already_owned"] is True
        assert _run(m, owned=False, force=True)["already_owned"] is False

    def test_same_mode_is_served_from_cache(self, m):
        first = _run(m, owned=False, force=True)
        again = _run(m, owned=False)
        assert first["from_cache"] is False and again["from_cache"] is True
        assert again["decision"] == first["decision"]

    def test_an_owned_decision_is_not_served_to_a_not_owned_request(self, m):
        owned = _run(m, owned=True, force=True)
        assert owned["decision"] == "SELL"
        other = _run(m, owned=False)
        assert other["from_cache"] is False
        assert other["decision"] == "DO NOT BUY" and other["already_owned"] is False

    def test_a_not_owned_decision_is_not_served_to_an_owned_request(self, m):
        _run(m, owned=False, force=True)
        other = _run(m, owned=True)
        assert other["from_cache"] is False
        assert other["decision"] == "SELL"

    def test_a_cache_entry_from_before_the_marker_serves_not_owned_only(self, m):
        old = _run(m, owned=False, force=True)
        old.pop("already_owned")
        old.pop("from_cache", None)
        m._decide_mem_cache["LOWCO"] = (10 ** 12, old)
        assert _run(m, owned=False)["from_cache"] is True
        m._decide_mem_cache["LOWCO"] = (10 ** 12, old)
        assert _run(m, owned=True)["from_cache"] is False


class TestFinalDecisionRecord:
    @pytest.fixture()
    def final_buy(self, m, monkeypatch):
        """Legacy scorer says DO NOT BUY (low pillars); the multi-horizon scorer, the primary decision, says BUY NOW."""
        monkeypatch.setattr(m, "multi_horizon_decide", lambda **k: {
            "decision": "BUY NOW", "combined_score": 80, "confidence": "High", "horizons": {}, "final_verdict": "x",
            "holding_period": "1-2 weeks"})
        monkeypatch.setattr(m, "_apply_data_quality_gate", lambda d, q, o, technical=None: d)
        monkeypatch.setattr(m, "_ms_apply_policy", None)
        return m

    def test_a_final_buy_the_legacy_scorer_did_not_flag_is_recorded(self, final_buy):
        bg = _BG()
        out = _run(final_buy, force=True, bg=bg)
        assert out["decision"] == "BUY NOW"
        recs = [c for c in bg.calls if c[0] is final_buy.record_prediction_for_training]
        assert len(recs) == 1
        assert recs[0][2]["decision"] == "BUY NOW" and recs[0][2]["symbol"] == "LOWCO"

    def test_switch_off_restores_the_legacy_gate(self, final_buy, monkeypatch):
        monkeypatch.setenv("DECIDE_RECORD_ON_FINAL_DECISION", "0")
        bg = _BG()
        _run(final_buy, force=True, bg=bg)
        assert [c for c in bg.calls if c[0] is final_buy.record_prediction_for_training] == []

    def test_a_final_non_buy_is_still_never_recorded(self, m):
        bg = _BG()
        out = _run(m, force=True, bg=bg)
        assert out["decision"] == "DO NOT BUY"
        assert [c for c in bg.calls if c[0] is m.record_prediction_for_training] == []

    def test_switch_parsing(self, m, monkeypatch):
        assert m._train_record_on_final_decision() is True
        for off in ("0", "false", "OFF", "no"):
            monkeypatch.setenv("DECIDE_RECORD_ON_FINAL_DECISION", off)
            assert m._train_record_on_final_decision() is False
        for on in ("1", "", " ", "yes"):
            monkeypatch.setenv("DECIDE_RECORD_ON_FINAL_DECISION", on)
            assert m._train_record_on_final_decision() is True
