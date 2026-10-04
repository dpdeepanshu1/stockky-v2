"""
tests/test_gemini_no_text.py -- group 144: prediction/main.py::_call_gemini with a 200 response
that has no text part (finishReason SAFETY / MAX_TOKENS, or a blocked prompt) must return None
with an INFO line, not raise KeyError('parts') into the "Gemini call failed" warning.
Run from services/decision-prediction-service/prediction:  python3 -m pytest tests/test_gemini_no_text.py
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys

import pytest

_PRED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def pm():
    added = _PRED_DIR not in sys.path
    if added:
        sys.path.insert(0, _PRED_DIR)
    before = set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location("_prediction_main_gemini_test", os.path.join(_PRED_DIR, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        if added and _PRED_DIR in sys.path:
            sys.path.remove(_PRED_DIR)
        for k in set(sys.modules) - before:
            if k.startswith("pred_") or k == "_prediction_main_gemini_test":
                sys.modules.pop(k, None)


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _call(pm, monkeypatch, payload):
    monkeypatch.setattr(pm, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(pm.httpx, "post", lambda *a, **k: _Resp(payload))
    return pm._call_gemini("sys", "user")


def test_normal_text_is_returned_stripped(pm, monkeypatch):
    p = {"candidates": [{"content": {"parts": [{"text": "  Strong setup.  "}]}}]}
    assert _call(pm, monkeypatch, p) == "Strong setup."


@pytest.mark.parametrize("payload", [
    {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"role": "model"}}]},   # no 'parts' (the logged KeyError)
    {"candidates": [{"finishReason": "SAFETY"}]},                                      # no content at all
    {"candidates": [{"content": {"parts": []}}]},
    {"candidates": [{"content": {"parts": [{"text": "   "}]}}]},
    {"candidates": []},
    {"promptFeedback": {"blockReason": "SAFETY"}},                                     # blocked prompt, no candidates key
])
def test_no_text_returns_none_without_raising(pm, monkeypatch, caplog, payload):
    with caplog.at_level(logging.INFO):
        assert _call(pm, monkeypatch, payload) is None
    assert not any("Gemini call failed" in r.getMessage() for r in caplog.records)
    assert any("Gemini returned no text" in r.getMessage() for r in caplog.records)


def test_reason_is_logged(pm, monkeypatch, caplog):
    p = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {}}]}
    with caplog.at_level(logging.INFO):
        _call(pm, monkeypatch, p)
    assert any("MAX_TOKENS" in r.getMessage() for r in caplog.records)
