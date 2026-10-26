"""
E218 - market-data /quotes/bulk, AngelOne REST stage: `_angelone_live_price` returns the row's `ltp` only. It used to fall back to `close` (the previous
close) and `open` when `ltp` was 0 and the row was published as a live price with fetched_at = now, so callers' quote-age checks saw it as fresh.
QUOTE_BULK_ANGELONE_CLOSE_FALLBACK=1 restores the old fallback.

Run from services/market-data-service:
    python3 -m pytest tests/test_e218_angelone_rest_ltp_only.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


def test_ltp_is_used(monkeypatch):
    monkeypatch.delenv("QUOTE_BULK_ANGELONE_CLOSE_FALLBACK", raising=False)
    assert main._angelone_live_price({"ltp": 101.5, "close": 95, "open": 99}) == 101.5


def test_zero_missing_or_bad_ltp_gives_none_not_close_or_open(monkeypatch):
    monkeypatch.delenv("QUOTE_BULK_ANGELONE_CLOSE_FALLBACK", raising=False)
    for row in ({"ltp": 0, "close": 95, "open": 99}, {"close": 95}, {"ltp": None, "open": 99},
                {"ltp": "x", "close": 95}, {"ltp": -3, "close": 95}, {"ltp": float("nan"), "close": 95}, {}, None):
        assert main._angelone_live_price(row) is None, row


def test_switch_restores_the_old_fallback(monkeypatch):
    monkeypatch.setenv("QUOTE_BULK_ANGELONE_CLOSE_FALLBACK", "1")
    assert main._angelone_live_price({"ltp": 0, "close": 95, "open": 99}) == 95.0
    assert main._angelone_live_price({"ltp": 0, "close": 0, "open": 99}) == 99.0
    assert main._angelone_live_price({"ltp": 0}) is None
