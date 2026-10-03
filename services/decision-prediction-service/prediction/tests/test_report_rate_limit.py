"""
tests/test_report_rate_limit.py — API_GATEWAY_URL handling in prediction/main.py::_report_rate_limit.

A blank / whitespace-only / slash-only API_GATEWAY_URL must count as "unset" (no POST), and a
padded real URL must be trimmed before "/ops/rate-limits/event" is appended.
Run from services/decision-prediction-service/prediction:  python3 -m pytest tests/test_report_rate_limit.py
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_PRED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def pm():
    # Load by path under a unique name so it cannot collide with other `main` modules, and
    # make the prediction dir importable for its sibling pred_* modules, then undo both.
    added = _PRED_DIR not in sys.path
    if added:
        sys.path.insert(0, _PRED_DIR)
    before = set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location("_prediction_main_under_test", os.path.join(_PRED_DIR, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        if added and _PRED_DIR in sys.path:
            sys.path.remove(_PRED_DIR)
        for k in set(sys.modules) - before:
            if k.startswith("pred_") or k == "_prediction_main_under_test":
                sys.modules.pop(k, None)


@pytest.mark.parametrize("raw", ["", "   ", "\t", " \n ", "/", "  /  "])
def test_blank_gateway_url_does_not_post(pm, monkeypatch, raw):
    monkeypatch.setenv("API_GATEWAY_URL", raw)
    called = []
    monkeypatch.setattr(pm.httpx, "post", lambda *a, **kw: called.append(a))
    pm._report_rate_limit("yfinance", 429, "/p", "d", "TCS")
    assert called == []


def test_unset_gateway_url_does_not_post(pm, monkeypatch):
    monkeypatch.delenv("API_GATEWAY_URL", raising=False)
    called = []
    monkeypatch.setattr(pm.httpx, "post", lambda *a, **kw: called.append(a))
    pm._report_rate_limit("yfinance", 429)
    assert called == []


@pytest.mark.parametrize("raw", ["  http://gw:1  ", "http://gw:1/ ", " http://gw:1/\n", "http://gw:1"])
def test_gateway_url_is_trimmed_and_payload_sent(pm, monkeypatch, raw):
    monkeypatch.setenv("API_GATEWAY_URL", raw)
    seen = {}

    def fake_post(url, **kw):
        seen["url"], seen["kw"] = url, kw

    monkeypatch.setattr(pm.httpx, "post", fake_post)
    pm._report_rate_limit("yfinance", 503, "/p", "x" * 500, "TCS")
    assert seen["url"] == "http://gw:1/ops/rate-limits/event"
    body = seen["kw"]["json"]
    assert body["source"] == "yfinance" and body["status"] == 503 and body["symbol"] == "TCS"
    assert len(body["detail"]) == 200
    assert seen["kw"]["timeout"] == 3.0


def test_post_failure_is_swallowed(pm, monkeypatch):
    monkeypatch.setenv("API_GATEWAY_URL", "http://gw:1")

    def boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(pm.httpx, "post", boom)
    pm._report_rate_limit("yfinance", 429)      # must not raise
