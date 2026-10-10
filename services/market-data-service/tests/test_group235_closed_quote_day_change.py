"""
group235: closed-market bhavcopy quotes carry the real previous close / day change / high / low / volume (they used
to carry previous_close == close, i.e. "0.0% today"), and the per-hit "Bhavcopy EOD waterfall hit" INFO line is
logged once per symbol and price instead of once per request.

Run from services/market-data-service:
    python3 -m pytest tests/test_group235_closed_quote_day_change.py -v
"""
from __future__ import annotations
import logging, os, sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import bhavcopy as bh
import main

_HDR = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, "
        "AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER")
_CSV = "\n".join([
    _HDR,
    "RELIANCE, EQ, 07-Oct-2026, 1400.00, 1405.00, 1420.00, 1398.00, 1415.50, 1417.25, 1410.00, 5000000, 70500.00, 90000, 2500000, 50.00",
    "DROPPER, EQ, 07-Oct-2026, 200.00, 199.00, 200.00, 180.00, 182.00, 180.00, 185.00, 1000, 1.00, 10, -, -",
    "NOPREV, EQ, 07-Oct-2026, -, 10.00, 11.00, 9.00, 10.50, 10.00, 10.00, 500, 1.00, 5, -, -",
]) + "\n"


class _Resp:
    status_code = 200
    headers = {"content-type": "text/csv"}

    def __init__(self, text):
        self.text, self.content = text, text.encode()


class _Client:
    def __init__(self, text=_CSV):
        self.text = text

    def get(self, *a, **k):
        return _Resp(self.text)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for d in (bh._BHAV_DAY_CACHE, bh._EOD_MISS, bh._BHAV_INFLIGHT, main._BHAV_HIT_LOGGED):
        d.clear()
    bh.MAX_STOCK_PRICE = 0.0
    monkeypatch.setattr(bh, "_nse_client", lambda: _Client())
    monkeypatch.setattr(main, "_cache_get", lambda k: None)
    monkeypatch.setattr(main, "_fallback_get", lambda k: None)
    yield
    for d in (bh._BHAV_DAY_CACHE, bh._EOD_MISS, bh._BHAV_INFLIGHT, main._BHAV_HIT_LOGGED):
        d.clear()


def test_parser_reads_prev_close_high_low_volume():
    r = bh._parse_bhav_csv_all(_CSV)["RELIANCE"]
    assert (r["prev_close"], r["day_high"], r["day_low"], r["volume"]) == (1400.0, 1420.0, 1398.0, 5000000.0)
    assert bh._parse_bhav_csv_all(_CSV)["NOPREV"]["prev_close"] is None      # "-" is not a number


def test_eod_row_has_session_date_and_matches_eod_close():
    row = bh.eod_row_from_bhavcopy("RELIANCE.NS")
    assert row["close"] == 1417.25 and row["prev_close"] == 1400.0 and row["date"]
    assert bh.eod_close_from_bhavcopy("RELIANCE") == 1417.25
    assert bh.eod_row_from_bhavcopy("NOSUCH") is None


def test_closed_quote_carries_real_day_change():
    q = main._closed_last_close_row("RELIANCE.NS")
    assert q["source"] == "bhavcopy_eod"
    assert q["price"] == 1417.25 and q["previous_close"] == 1400.0
    assert q["day_change_pct"] == pytest.approx(1.23, abs=0.01)
    assert q["day_high"] == 1420.0 and q["day_low"] == 1398.0 and q["volume"] == 5000000


def test_closed_quote_negative_day_change():
    q = main._closed_last_close_row("DROPPER")
    assert q["previous_close"] == 200.0 and q["day_change_pct"] == -10.0


def test_missing_prev_close_keeps_old_shape():
    q = main._closed_last_close_row("NOPREV")
    assert q["price"] == 10.0 and q["previous_close"] == 10.0 and q["day_change_pct"] is None


def test_unknown_symbol_is_none():
    assert main._closed_last_close_row("NOSUCH") is None


def test_extras_ignored_when_row_close_disagrees_with_price(monkeypatch):
    monkeypatch.setattr(main, "_waterfall_bhavcopy_price", lambda s: 999.0)
    q = main._closed_last_close_row("RELIANCE")
    assert q["price"] == 999.0 and q["previous_close"] == 999.0 and q["day_change_pct"] is None


def test_extras_failure_never_breaks_the_quote(monkeypatch):
    def _boom(sym):
        raise RuntimeError("x")
    monkeypatch.setattr(main, "_waterfall_bhavcopy_price", lambda s: 1417.25)
    monkeypatch.setattr(bh, "eod_row_from_bhavcopy", _boom)
    q = main._closed_last_close_row("RELIANCE")
    assert q["price"] == 1417.25 and q["previous_close"] == 1417.25


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def _capture():
    h, old = _Cap(), main.logger.level
    main.logger.addHandler(h)
    main.logger.setLevel(logging.INFO)
    return h, old


def _release(h, old):
    main.logger.removeHandler(h)
    main.logger.setLevel(old)


def test_hit_logged_once_per_symbol_and_price(monkeypatch):
    monkeypatch.setenv("BHAVCOPY_HIT_LOG_PER_SYMBOL", "1")  # group302: per-symbol INFO is now opt-in
    h, old = _capture()
    try:
        for _ in range(5):
            main._waterfall_bhavcopy_price("RELIANCE")
        main._waterfall_bhavcopy_price("DROPPER")
    finally:
        _release(h, old)
    hits = [m for m in h.msgs if "Bhavcopy EOD waterfall hit" in m]
    assert len(hits) == 2 and any("RELIANCE" in h for h in hits) and any("DROPPER" in h for h in hits)


def test_hit_logged_again_when_price_changes(monkeypatch):
    monkeypatch.setenv("BHAVCOPY_HIT_LOG_PER_SYMBOL", "1")  # group302: per-symbol INFO is now opt-in
    h, old = _capture()
    try:
        main._log_bhavcopy_hit_once("ABC", 10.0)
        main._log_bhavcopy_hit_once("ABC", 10.0)
        main._log_bhavcopy_hit_once("ABC", 11.0)
    finally:
        _release(h, old)
    assert len([m for m in h.msgs if "ABC" in m and "hit" in m]) == 2
