"""
group191 (item 17): "CANDIDATE REJECTED" log lines were cut at 150 characters (HFCL ended mid-dict, REDINGTON at
"Wait for a breakout or"). The limit is now CANDIDATE_REJECT_LOG_MAX (default 500, 0 = never cut) and a cut line ends
in " ...".

Run from services/real-trade-service:
    python3 -m pytest tests/test_group191_reject_log_not_cut.py -q
"""
from __future__ import annotations
import inspect, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from candidate_engine import candidates as c

HFCL = ("Weighted bullish score 1.5 (need ≥4, threshold >0.5%). Returns: {'1d': 1.2, '1w': None, '1m': 4.72, "
        "'3m': 17.22, '6m': None, '1y': None, '2y': None}. 1-day down-weighted (0.5x) — single-day pop is "
        "mean-reversion risk.")
REDINGTON = ("Price ₹312.40 is within 2% of 20-day high — near resistance. In a ranging/choppy market buying near "
             "resistance gives poor R:R. Wait for a breakout or pullback.")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CANDIDATE_REJECT_LOG_MAX", raising=False)


def test_the_two_real_reasons_are_longer_than_the_old_cut_and_now_whole():
    assert len(HFCL) > 150 and len(REDINGTON) > 150
    assert c._reject_log_text(HFCL) == HFCL
    assert c._reject_log_text(REDINGTON) == REDINGTON
    assert c._reject_log_text(HFCL).endswith("mean-reversion risk.")


def test_long_reason_is_cut_visibly(monkeypatch):
    monkeypatch.setenv("CANDIDATE_REJECT_LOG_MAX", "40")
    out = c._reject_log_text("x" * 100)
    assert out == "x" * 40 + " ..."


def test_zero_means_never_cut(monkeypatch):
    monkeypatch.setenv("CANDIDATE_REJECT_LOG_MAX", "0")
    assert c._reject_log_text("y" * 5000) == "y" * 5000


def test_bad_value_falls_back_to_500(monkeypatch):
    monkeypatch.setenv("CANDIDATE_REJECT_LOG_MAX", "abc")
    assert c._reject_log_text("z" * 600) == "z" * 500 + " ..."
    assert c._reject_log_text("z" * 500) == "z" * 500


def test_non_string_input_does_not_raise():
    assert c._reject_log_text(None) == "None"


def test_both_log_sites_use_the_helper():
    src = inspect.getsource(c)
    assert "reject[:150]" not in src.replace("`reject[:150]`", "")
    assert src.count("| %s\", sym, mode, _reject_log_text(reject)") == 2
