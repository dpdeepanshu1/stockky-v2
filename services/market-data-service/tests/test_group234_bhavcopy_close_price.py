"""
group234: NSE's sec_bhavdata_full names the close column CLOSE_PRICE (and LAST_PRICE). The parsers only knew
"ClosePrice", so every row had close=None, eod_close_from_bhavcopy() found no symbol, and group233's closed-market
last-close path fell through to AngelOne/Yahoo. Also: the per-date day fetch is single-flight, and the latest
session is preloaded at boot.

Run from services/market-data-service:
    python3 -m pytest tests/test_group234_bhavcopy_close_price.py -v
"""
from __future__ import annotations
import logging, os, sys, threading, time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import bhavcopy as bh


# The real file's header and values carry leading spaces after every comma.
_NSE_HEADER = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, "
               "AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER")
_NSE_CSV = "\n".join([
    _NSE_HEADER,
    "RELIANCE, EQ, 07-Oct-2026, 1400.00, 1405.00, 1420.00, 1398.00, 1415.50, 1417.25, 1410.00, 5000000, 70500.00, 90000, 2500000, 50.00",
    "TCS, EQ, 07-Oct-2026, 3200.00, 3210.00, 3250.00, 3190.00, 3240.00, 3244.10, 3230.00, 2000000, 64600.00, 50000, 1000000, 50.00",
    "SOMEETF, EQ, 07-Oct-2026, 100.00, 100.00, 101.00, 99.00, 100.50, 100.60, 100.20, 1000, 1.00, 10, -, -",
    "FUTX, FU, 07-Oct-2026, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, 1, 1.00, 1, -, -",
]) + "\n"


class _Resp:
    def __init__(self, text, status=200):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = {"content-type": "text/csv"}


class _Client:
    def __init__(self, text=_NSE_CSV, delay=0.0):
        self.text, self.delay, self.calls = text, delay, 0
        self._lock = threading.Lock()

    def get(self, url, *a, **kw):
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return _Resp(self.text)


@pytest.fixture(autouse=True)
def _clean():
    bh._BHAV_DAY_CACHE.clear()
    bh._EOD_MISS.clear()
    bh._NO_CLOSE_WARNED.clear()
    bh._BHAV_INFLIGHT.clear()
    bh.MAX_STOCK_PRICE = 0.0
    yield
    bh._BHAV_DAY_CACHE.clear()
    bh._EOD_MISS.clear()
    bh._NO_CLOSE_WARNED.clear()
    bh._BHAV_INFLIGHT.clear()
    bh.MAX_STOCK_PRICE = 0.0


def test_real_nse_header_close_is_parsed():
    out = bh._parse_bhav_csv_all(_NSE_CSV)
    assert out["RELIANCE"]["close"] == 1417.25          # CLOSE_PRICE, not LAST_PRICE (1415.50)
    assert out["TCS"]["close"] == 3244.10
    assert out["SOMEETF"]["close"] == 100.60            # kept even with no delivery %
    assert "FUTX" not in out                            # non EQ/BE/BZ series still skipped


def test_single_symbol_parser_reads_close_price_too():
    row = bh._parse_bhav_csv(_NSE_CSV, "RELIANCE.NS")
    assert row is not None and row["close"] == 1417.25


def test_close_column_wins_over_last_price_when_both_exist():
    csv_text = "SYMBOL,SERIES,LAST_PRICE,CLOSE\nABC,EQ,10,12\n"
    assert bh._parse_bhav_csv_all(csv_text)["ABC"]["close"] == 12.0


def test_legacy_and_udiff_headers_unchanged():
    assert bh._parse_bhav_csv_all("SYMBOL,SERIES,CLOSE\nA,EQ,5\n")["A"]["close"] == 5.0
    assert bh._parse_bhav_csv_all("TckrSymb,SctySrs,ClsPric\nA,EQ,6\n")["A"]["close"] == 6.0
    assert bh._parse_bhav_csv_all("SYMBOL,SERIES,ClosePrice\nA,EQ,7\n")["A"]["close"] == 7.0


def test_eod_close_from_bhavcopy_finds_symbols_end_to_end(monkeypatch):
    monkeypatch.setattr(bh, "_nse_client", lambda: _Client())
    assert bh.eod_close_from_bhavcopy("RELIANCE") == 1417.25
    assert bh.eod_close_from_bhavcopy("tcs.ns") == 3244.10
    assert bh.eod_close_from_bhavcopy("NOSUCHSYM") is None


def test_missing_close_column_warns_once():
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _H()
    bh.logger.addHandler(h)
    try:
        txt = "SYMBOL,SERIES,DELIV_PER\nA,EQ,40\n"
        bh._parse_bhav_csv_all(txt)
        bh._parse_bhav_csv_all(txt)
    finally:
        bh.logger.removeHandler(h)
    assert len([m for m in records if "no close-price column" in m]) == 1


def test_day_fetch_is_single_flight():
    client = _Client(delay=0.25)
    d = date(2026, 10, 7)
    results = []

    def _go():
        results.append(bh._fetch_bhav_day_parsed(client, d))

    threads = [threading.Thread(target=_go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 8 and all(r and "RELIANCE" in r for r in results)
    assert client.calls == 1, "eight concurrent callers must share one download"


def test_failed_leader_is_not_remembered():
    d = date(2026, 10, 7)

    class _Down:
        def get(self, *a, **k):
            return _Resp("", status=503)

    assert bh._fetch_bhav_day_parsed(_Down(), d) is None
    assert bh._BHAV_INFLIGHT == {}
    assert bh._fetch_bhav_day_parsed(_Client(), d) is not None      # next caller simply tries again


def test_prewarm_latest_day_caches_the_newest_session(monkeypatch):
    monkeypatch.setattr(bh, "_nse_client", lambda: _Client())
    iso = bh.prewarm_latest_day()
    assert iso is not None and iso in bh._BHAV_DAY_CACHE
    assert bh._BHAV_DAY_CACHE[iso]["RELIANCE"]["close"] == 1417.25


def test_prewarm_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("nse down")
    monkeypatch.setattr(bh, "_nse_client", _boom)
    assert bh.prewarm_latest_day() is None
