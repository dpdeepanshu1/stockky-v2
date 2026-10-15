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
            "ms_room_atr": 3.0, "ms_rr": 2.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": [], "ms_tqs": 70, "ms_groups": 3, "ms_loc_score": 70,
            "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False}
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
    r = _post("full")
    assert "structure" in r and r["structure"]["mode"] == "full"
    assert r["structure"]["original_decision"] == r["decision"] or r["structure"]["applied"] is True


def test_old_shadow_value_is_live_not_record_only():
    # group370: a stale STRUCTURE_POLICY_MODE=shadow in an old .env must NOT make the policy record-only any more
    r = _post("shadow")
    st = r["structure"]
    assert st["mode"] == "full" and st["original_decision"] == "BUY NOW"
    assert r["decision"] == "WAIT" and st["policy_decision"] == "WAIT" and st["applied"] is True
    assert not any("shadow" in x for x in r["reasons"]["structure"])


def test_full_downgrades_chase_and_records_original():
    r = _post("full")
    st = r["structure"]
    assert st["original_decision"] == "BUY NOW"
    assert r["decision"] == "WAIT" and st["applied"] is True and r["confidence"] == "Low"


def test_off_kill_switch_leaves_the_decision_alone():
    r = _post("off")
    assert r["decision"] == r["structure"]["original_decision"] and r["structure"]["applied"] is False


def test_missing_summary_is_no_opinion_not_an_error():
    p = _payload()
    p.pop("structure_summary")
    os.environ["STRUCTURE_POLICY_MODE"] = "full"
    try:
        m = _load()
        with TestClient(m.app) as c:
            r = c.post("/decide/evaluate", json=p).json()
    finally:
        os.environ.pop("STRUCTURE_POLICY_MODE", None)
    st = r["structure"]
    assert st["no_opinion"]
    # group377: a BUY NOW with no structure waits; any other decision is untouched
    if st["original_decision"] == "BUY NOW":
        assert r["decision"] == "WAIT" and [x["id"] for x in st["rules"]] == ["P1_NO_STRUCTURE"]
    else:
        assert r["decision"] == st["original_decision"]
