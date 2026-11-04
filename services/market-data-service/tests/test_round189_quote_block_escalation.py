"""Round 189 (issue #4): a 403 straight after a pause doubles the next quote-equity pause (cap NSE_QUOTE_BLOCK_MAX_S); a 200 resets it.

Run from services/market-data-service:  python3 -m pytest tests/test_round189_quote_block_escalation.py -q
"""
from __future__ import annotations

import time

import bhavcopy as bh


def _reset():
    bh._QUOTE_BLOCK.update({"until": 0.0, "status": None, "skipped": 0, "streak": 0})


def _secs_left():
    return bh._QUOTE_BLOCK["until"] - time.time()


def _expire():
    bh._QUOTE_BLOCK["until"] = time.time() - 1


def test_pauses_double_and_cap(monkeypatch):
    monkeypatch.setenv("NSE_QUOTE_BLOCK_SECONDS", "600")
    monkeypatch.delenv("NSE_QUOTE_BLOCK_MAX_S", raising=False)
    _reset()
    got = []
    for _ in range(5):
        bh._note_quote_status(403)
        got.append(round(_secs_left() / 10) * 10)
        _expire()
    assert got == [600, 1200, 2400, 3600, 3600]


def test_success_resets_the_streak(monkeypatch):
    monkeypatch.setenv("NSE_QUOTE_BLOCK_SECONDS", "600")
    _reset()
    bh._note_quote_status(403); _expire()
    bh._note_quote_status(403); _expire()
    bh._note_quote_status(200)
    bh._note_quote_status(403)
    assert abs(_secs_left() - 600) < 5 and bh._QUOTE_BLOCK["streak"] == 1


def test_403_during_a_pause_does_not_count_or_shorten(monkeypatch):
    monkeypatch.setenv("NSE_QUOTE_BLOCK_SECONDS", "600")
    _reset()
    bh._note_quote_status(403); _expire()
    bh._note_quote_status(403)                      # second block: 1200 s
    bh._note_quote_status(403)                      # an in-flight call lands during it
    assert bh._QUOTE_BLOCK["streak"] == 2 and _secs_left() > 1190


def test_cap_never_below_base(monkeypatch):
    monkeypatch.setenv("NSE_QUOTE_BLOCK_SECONDS", "600")
    monkeypatch.setenv("NSE_QUOTE_BLOCK_MAX_S", "100")
    assert bh._quote_pause_for(600, 4) == 600


def test_zero_base_disables(monkeypatch):
    monkeypatch.setenv("NSE_QUOTE_BLOCK_SECONDS", "0")
    _reset()
    bh._note_quote_status(403)
    assert not bh.nse_quote_blocked()


def test_other_statuses_never_pause(monkeypatch):
    monkeypatch.setenv("NSE_QUOTE_BLOCK_SECONDS", "600")
    _reset()
    bh._note_quote_status(404); bh._note_quote_status(503)
    assert not bh.nse_quote_blocked() and bh._QUOTE_BLOCK["streak"] == 0
