"""Round 191: a FAILED overseas-cue fetch (ES=F blip) is remembered GLOBAL_CUE_ERROR_TTL_S (30 s) instead of the full 300 s cue TTL; a clean
"no usable bar" miss still keeps the full TTL.

Run from services/market-data-service:  python3 -m pytest tests/test_round191_global_cue_error_ttl.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

import main


def _hist(closes):
    last = datetime.now(timezone.utc).date()
    idx = pd.to_datetime([last - timedelta(days=len(closes) - 1 - i) for i in range(len(closes))])
    return pd.DataFrame({"Close": closes}, index=idx)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    main._GLOBAL_CUE_STATE["rows"].clear()
    for k in ("GLOBAL_CUE_ALLOWED", "GLOBAL_CUE_TTL_S", "GLOBAL_CUE_MAX_BAR_AGE_DAYS", "GLOBAL_CUE_ERROR_TTL_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(main, "_in_cooldown", lambda name: False)
    yield
    main._GLOBAL_CUE_STATE["rows"].clear()


def _patch(monkeypatch, script, calls):
    """script: list of results per call of ES=F (an Exception is raised, anything else returned)."""
    class T:
        def __init__(self, sym):
            self.sym = sym

        def history(self, period=None, interval=None):
            calls.append(self.sym)
            v = script[min(len(calls) - 1, len(script) - 1)]
            if isinstance(v, Exception):
                raise v
            return v
    monkeypatch.setattr(main.yf, "Ticker", T)


def _age(monkeypatch, secs):
    """Pretend `secs` seconds passed since every cached row was stored."""
    real = main.time.monotonic
    monkeypatch.setattr(main.time, "monotonic", lambda: real() + secs)


def test_error_is_retried_after_30s_not_300s(monkeypatch):
    calls = []
    _patch(monkeypatch, [RuntimeError("blip"), _hist([5000.0, 5050.0])], calls)
    assert main.get_global_cues(symbols="ES=F")["missing"] == ["ES=F"]
    assert main.get_global_cues(symbols="ES=F")["missing"] == ["ES=F"] and len(calls) == 1      # inside 30 s: not asked again
    _age(monkeypatch, 31)
    body = main.get_global_cues(symbols="ES=F")
    assert len(calls) == 2 and "ES=F" in body["rows"]


def test_clean_miss_keeps_the_full_ttl(monkeypatch):
    calls = []
    _patch(monkeypatch, [None, _hist([5000.0, 5050.0])], calls)
    main.get_global_cues(symbols="ES=F")
    _age(monkeypatch, 31)
    main.get_global_cues(symbols="ES=F")
    assert len(calls) == 1                                  # still cached at 31 s
    _age(monkeypatch, 301)
    main.get_global_cues(symbols="ES=F")
    assert len(calls) == 2


def test_error_ttl_zero_retries_every_call(monkeypatch):
    monkeypatch.setenv("GLOBAL_CUE_ERROR_TTL_S", "0")
    calls = []
    _patch(monkeypatch, [RuntimeError("x")], calls)
    main.get_global_cues(symbols="ES=F"); main.get_global_cues(symbols="ES=F")
    assert len(calls) == 2


def test_bad_error_ttl_falls_back_to_30(monkeypatch):
    monkeypatch.setenv("GLOBAL_CUE_ERROR_TTL_S", "soon")
    assert main._global_cue_error_ttl() == 30.0
