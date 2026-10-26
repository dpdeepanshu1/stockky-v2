"""E192: the decision service's error fallback (HTTP 200, neutral scores, `error_detail`) must never be cached or served as a decision.

The gateway cached any non-empty body under the shared decide key, and the service's weak-payload check did not recognise the fallback
(combined 45, a close, no "error processing" text), so one outage answer was served as real for DECIDE_CACHE_TTL_OPEN (12 h when closed).
hotpicks_repair_scores also wrote it into stored Hot Picks rows (tests in api-gateway/tests/test_hotpicks_store.py).
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

FALLBACK = {"symbol": "X", "decision": "DO NOT BUY", "combined_score": 45, "technical_score": 50, "close": 100.0,
            "reasons": {"technical": ["Decision engine error: boom"]}, "error_detail": "boom"}


@pytest.fixture()
def m(monkeypatch):
    spec = importlib.util.spec_from_file_location("decision_main_e192", os.path.join(_ROOT, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._decide_mem_cache.clear()
    monkeypatch.setattr(mod, "_redis", None)
    monkeypatch.setattr(mod, "_kv", None)
    return mod


def test_error_fallback_is_weak(m):
    assert m._is_weak_decide_payload(dict(FALLBACK)) is True


def test_error_fallback_is_not_cached(m):
    m._cache_set_decide("X", dict(FALLBACK))
    assert m._cache_get_decide("X") is None


def test_real_decision_still_cached(m):
    real = {k: v for k, v in FALLBACK.items() if k not in ("error_detail", "reasons")}
    assert m._is_weak_decide_payload(real) is False
    m._cache_set_decide("X", real)
    assert m._cache_get_decide("X") == real


def test_gateway_does_not_cache_the_error_fallback():
    src = (Path(_ROOT).parents[1] / "api-gateway" / "main.py").read_text(encoding="utf-8")
    assert 'if isinstance(raw, dict) and raw and not raw.get("error_detail"):' in src
