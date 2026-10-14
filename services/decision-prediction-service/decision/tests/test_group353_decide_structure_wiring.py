"""group353: /decide/evaluate carries the structure policy end to end (skip_http payload, no network)."""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _load():
    spec = importlib.util.spec_from_file_location("decision_main_g353", os.path.join(_ROOT, "main.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _payload(**ms):
    summ = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.0, "ms_atr": 2.0, "ms_chase_atr": 3.1, "ms_stretch_atr": 1.0,
            "ms_room_atr": 3.0, "ms_rr": 2.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": [], "ms_tqs": 70, "ms_groups": 3}
    summ.update(ms)
    return {"symbol": "TESTCO", "skip_http": True, "force": True, "technical_score": 85, "fundamental_score": 85, "news_score": 80,
            "rsi": 58, "close": 100.0, "support": 96.0, "resistance": 125.0, "trend_strength": "strong", "volume_surge": True,
            "market_score": 70, "sentiment_score": 70, "structure_summary": summ}


def _post(mode, **ms):
    os.environ["STRUCTURE_POLICY_MODE"] = mode
    try:
        m = _load()
        with TestClient(m.app) as c:
            return c.post("/decide/evaluate", json=_payload(**ms)).json()
    finally:
        os.environ.pop("STRUCTURE_POLICY_MODE", None)


def test_structure_block_always_present_and_additive():
    r = _post("shadow")
    assert "structure" in r and r["structure"]["mode"] == "shadow"
    assert r["structure"]["original_decision"] == r["decision"] or r["structure"]["applied"] is False


def test_shadow_does_not_change_decision_but_records_would_be():
    r = _post("shadow")
    st = r["structure"]
    assert st["original_decision"] == "BUY NOW"
    if True:
        assert r["decision"] == "BUY NOW" and st["policy_decision"] == "WAIT" and st["applied"] is False
        assert any("shadow" in x for x in r["reasons"]["structure"])


def test_caps_downgrades_chase_and_records_original():
    r = _post("caps")
    st = r["structure"]
    assert st["original_decision"] == "BUY NOW"
    if True:
        assert r["decision"] == "WAIT" and st["applied"] is True and r["confidence"] == "Low"


def test_missing_summary_is_no_opinion_not_an_error():
    p = _payload()
    p.pop("structure_summary")
    os.environ["STRUCTURE_POLICY_MODE"] = "caps"
    try:
        m = _load()
        with TestClient(m.app) as c:
            r = c.post("/decide/evaluate", json=p).json()
    finally:
        os.environ.pop("STRUCTURE_POLICY_MODE", None)
    assert r["structure"]["no_opinion"] and r["decision"] == r["structure"]["original_decision"]
