"""
tests/test_bhavcopy.py — coverage for bhavcopy.py

No real NSE network calls. httpx.Client is monkeypatched.
pandas is NOT required — all dataframe-path tests use the pure-Python routes.

Run from services/market-data-service:
    python3 -m pytest tests/test_bhavcopy.py -v
"""
from __future__ import annotations
import csv, io, os, sys, time, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import httpx
import bhavcopy as bh


# ── helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean():
    bh._BHAV_DAY_CACHE.clear()
    bh._NSE_SESSION_CACHE["client"] = None
    bh._NSE_SESSION_CACHE["ts"] = 0.0
    bh._denied_last_logged = {} if hasattr(bh, "_denied_last_logged") else {}
    bh.MAX_STOCK_PRICE = 0.0
    yield
    bh._BHAV_DAY_CACHE.clear()
    bh._NSE_SESSION_CACHE["client"] = None
    bh._NSE_SESSION_CACHE["ts"] = 0.0
    bh.MAX_STOCK_PRICE = 0.0


class _FakeResp:
    def __init__(self, status=200, text="", content=None, headers=None):
        self.status_code = status
        self.text = text
        self.content = content if content is not None else text.encode()
        self.headers = headers or {}
    def json(self): import json; return json.loads(self.text)


def _csv_text(rows, header):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header)
    w.writeheader(); w.writerows(rows)
    return buf.getvalue()


def _zip_of_csv(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bhav.csv", csv_text)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# _nse_client — caching
# ══════════════════════════════════════════════════════════════════════════════

class TestNseClient:
    def test_returns_cached_client_within_ttl(self, monkeypatch):
        fake = object()
        bh._NSE_SESSION_CACHE["client"] = fake
        bh._NSE_SESSION_CACHE["ts"] = time.time()
        monkeypatch.setattr(httpx, "Client", lambda **kw: (_ for _ in ()).throw(AssertionError("should not build")))
        assert bh._nse_client() is fake

    def test_builds_new_client_when_stale(self, monkeypatch):
        bh._NSE_SESSION_CACHE["ts"] = 0.0

        class _FakeClient:
            def get(self, *a, **kw): return _FakeResp()
            def close(self): pass

        calls = []
        monkeypatch.setattr(httpx, "Client", lambda **kw: (calls.append(1), _FakeClient())[1])
        c = bh._nse_client()
        assert calls and c is not None

    def test_force_new_ignores_cache(self, monkeypatch):
        bh._NSE_SESSION_CACHE["ts"] = time.time()

        class _FakeClient:
            def get(self, *a, **kw): return _FakeResp()
            def close(self): pass

        calls = []
        monkeypatch.setattr(httpx, "Client", lambda **kw: (calls.append(1), _FakeClient())[1])
        bh._nse_client(force_new=True)
        assert calls

    def test_bootstrap_exception_is_swallowed(self, monkeypatch):
        bh._NSE_SESSION_CACHE["ts"] = 0.0

        class _BrokenClient:
            def get(self, *a, **kw): raise RuntimeError("network down")
            def close(self): pass

        monkeypatch.setattr(httpx, "Client", lambda **kw: _BrokenClient())
        c = bh._nse_client()    # must not raise
        assert c is not None

    def test_closes_old_client_on_refresh(self, monkeypatch):
        closed = []

        class _OldClient:
            def close(self): closed.append(True)

        class _NewClient:
            def get(self, *a, **kw): return _FakeResp()
            def close(self): pass

        bh._NSE_SESSION_CACHE["client"] = _OldClient()
        bh._NSE_SESSION_CACHE["ts"] = 0.0
        monkeypatch.setattr(httpx, "Client", lambda **kw: _NewClient())
        bh._nse_client()
        assert closed


# ══════════════════════════════════════════════════════════════════════════════
# _candidate_session_dates
# ══════════════════════════════════════════════════════════════════════════════

class TestCandidateSessionDates:
    def test_returns_n_weekdays(self):
        dates = bh._candidate_session_dates(5)
        assert len(dates) == 5
        for d in dates:
            assert d.weekday() < 5

    def test_descending_order(self):
        dates = bh._candidate_session_dates(4)
        for i in range(len(dates) - 1):
            assert dates[i] >= dates[i+1]


# ══════════════════════════════════════════════════════════════════════════════
# _bhav_urls_for_date
# ══════════════════════════════════════════════════════════════════════════════

class TestBhavUrlsForDate:
    def test_returns_nonempty_list(self):
        from datetime import date
        urls = bh._bhav_urls_for_date(date(2026, 8, 18))
        assert len(urls) >= 4

    def test_first_url_is_sec_bhavdata_full(self):
        from datetime import date
        urls = bh._bhav_urls_for_date(date(2026, 8, 18))
        assert "sec_bhavdata_full" in urls[0]

    def test_date_components_in_urls(self):
        from datetime import date
        urls = bh._bhav_urls_for_date(date(2026, 8, 18))
        joined = " ".join(urls)
        assert "18082026" in joined   # ddmmyyyy pattern


# ══════════════════════════════════════════════════════════════════════════════
# process_bhavcopy_rows
# ══════════════════════════════════════════════════════════════════════════════

class TestProcessBhavcopydRows:
    def test_keeps_eq_series(self):
        rows = [{"SYMBOL": "X", "SERIES": "EQ", "CLOSE": "100"}]
        assert bh.process_bhavcopy_rows(rows) == rows

    def test_keeps_be_series(self):
        rows = [{"SYMBOL": "X", "SERIES": "BE", "CLOSE": "100"}]
        assert len(bh.process_bhavcopy_rows(rows)) == 1

    def test_drops_non_eq_series(self):
        rows = [{"SYMBOL": "X", "SERIES": "SM", "CLOSE": "100"}]
        assert bh.process_bhavcopy_rows(rows) == []

    def test_drops_over_price_cap(self):
        bh.MAX_STOCK_PRICE = 100.0
        rows = [{"SYMBOL": "X", "SERIES": "EQ", "CLOSE": "200"}]
        assert bh.process_bhavcopy_rows(rows) == []

    def test_keeps_under_price_cap(self):
        bh.MAX_STOCK_PRICE = 5000.0
        rows = [{"SYMBOL": "X", "SERIES": "EQ", "CLOSE": "1000"}]
        assert len(bh.process_bhavcopy_rows(rows)) == 1

    def test_skips_non_dict_rows(self):
        assert bh.process_bhavcopy_rows(["not_a_dict"]) == []

    def test_comma_formatted_price(self):
        rows = [{"SYMBOL": "X", "CLOSE": "1,234.56"}]
        result = bh.process_bhavcopy_rows(rows)
        assert result  # kept (no cap configured)

    def test_na_price_is_not_filtered(self):
        rows = [{"SYMBOL": "X", "CLOSE": "NA"}]
        result = bh.process_bhavcopy_rows(rows)
        assert result  # no close parsed → no cap applied → kept

    def test_empty_input_returns_empty(self):
        assert bh.process_bhavcopy_rows([]) == []

    def test_none_input_returns_empty(self):
        assert bh.process_bhavcopy_rows(None) == []


# ══════════════════════════════════════════════════════════════════════════════
# _parse_bhav_csv_all  (cache-feed parser)
# ══════════════════════════════════════════════════════════════════════════════

_FULL_CSV = _csv_text([
    {"SYMBOL": "RELIANCE", "SERIES": "EQ", "CLOSE": "2500.00",
     "DELIV_PER": "55.2", "TTL_TRD_QNTY": "1000000"},
    {"SYMBOL": "TCS", "SERIES": "EQ", "CLOSE": "3200.00",
     "DELIV_PER": "62.1", "TTL_TRD_QNTY": "800000"},
    {"SYMBOL": "FUTURES25JAN", "SERIES": "FU", "CLOSE": "100.00",
     "DELIV_PER": "0", "TTL_TRD_QNTY": "100"},
], ["SYMBOL", "SERIES", "CLOSE", "DELIV_PER", "TTL_TRD_QNTY"])


class TestParseBhavCsvAll:
    def test_parses_eq_rows(self):
        out = bh._parse_bhav_csv_all(_FULL_CSV)
        assert "RELIANCE" in out and "TCS" in out

    def test_skips_non_eq_series(self):
        out = bh._parse_bhav_csv_all(_FULL_CSV)
        assert "FUTURES25JAN" not in out

    def test_delivery_pct_extracted(self):
        out = bh._parse_bhav_csv_all(_FULL_CSV)
        assert out["RELIANCE"]["delivery_pct"] == 55.2

    def test_close_price_extracted(self):
        out = bh._parse_bhav_csv_all(_FULL_CSV)
        assert out["TCS"]["close"] == 3200.0

    def test_empty_csv_returns_empty(self):
        assert bh._parse_bhav_csv_all("") == {}

    def test_bom_stripped(self):
        out = bh._parse_bhav_csv_all("\ufeff" + _FULL_CSV)
        assert "RELIANCE" in out

    def test_delivery_computed_from_qty_when_pct_absent(self):
        csv_text = _csv_text([
            {"SYMBOL": "INFY", "SERIES": "EQ", "CLOSE": "1500",
             "DELIV_QTY": "500000", "TTL_TRD_QNTY": "1000000"},
        ], ["SYMBOL", "SERIES", "CLOSE", "DELIV_QTY", "TTL_TRD_QNTY"])
        out = bh._parse_bhav_csv_all(csv_text)
        assert out["INFY"]["delivery_pct"] == pytest.approx(50.0)

    def test_price_cap_excludes_row(self):
        bh.MAX_STOCK_PRICE = 1000.0
        out = bh._parse_bhav_csv_all(_FULL_CSV)
        assert "RELIANCE" not in out   # 2500 > 1000
        assert "TCS" not in out        # 3200 > 1000

    def test_duplicate_symbol_first_kept(self):
        csv_text = _csv_text([
            {"SYMBOL": "HDFC", "SERIES": "EQ", "CLOSE": "1000", "DELIV_PER": "40"},
            {"SYMBOL": "HDFC", "SERIES": "BE", "CLOSE": "1001", "DELIV_PER": "41"},
        ], ["SYMBOL", "SERIES", "CLOSE", "DELIV_PER"])
        out = bh._parse_bhav_csv_all(csv_text)
        assert out["HDFC"]["close"] == 1000.0


# ══════════════════════════════════════════════════════════════════════════════
# _parse_bhav_csv  (single-symbol parser)
# ══════════════════════════════════════════════════════════════════════════════

class TestParseBhavCsv:
    def test_finds_symbol(self):
        result = bh._parse_bhav_csv(_FULL_CSV, "RELIANCE")
        assert result is not None
        assert result["delivery_pct"] == 55.2

    def test_returns_none_for_missing_symbol(self):
        assert bh._parse_bhav_csv(_FULL_CSV, "NOTEXIST") is None

    def test_empty_csv_returns_none(self):
        assert bh._parse_bhav_csv("", "RELIANCE") is None

    def test_non_eq_series_skipped(self):
        assert bh._parse_bhav_csv(_FULL_CSV, "FUTURES25JAN") is None

    def test_price_over_cap_returns_none(self):
        bh.MAX_STOCK_PRICE = 100.0
        assert bh._parse_bhav_csv(_FULL_CSV, "RELIANCE") is None

    def test_bom_handled(self):
        result = bh._parse_bhav_csv("\ufeff" + _FULL_CSV, "TCS")
        assert result is not None

    def test_delivery_from_qty_fallback(self):
        csv_text = _csv_text([
            {"SYMBOL": "WIPRO", "SERIES": "EQ", "CLOSE": "300",
             "DELIV_QTY": "200", "TTL_TRD_QNTY": "400"},
        ], ["SYMBOL", "SERIES", "CLOSE", "DELIV_QTY", "TTL_TRD_QNTY"])
        result = bh._parse_bhav_csv(csv_text, "WIPRO")
        assert result["delivery_pct"] == pytest.approx(50.0)

    def test_row_with_only_close_kept(self):
        csv_text = _csv_text([
            {"SYMBOL": "NTPC", "SERIES": "EQ", "CLOSE": "200"},
        ], ["SYMBOL", "SERIES", "CLOSE"])
        result = bh._parse_bhav_csv(csv_text, "NTPC")
        assert result is not None
        assert result["close"] == 200.0
        assert result["delivery_pct"] is None


# ══════════════════════════════════════════════════════════════════════════════
# _fetch_bhav_day_parsed — with fake HTTP client
# ══════════════════════════════════════════════════════════════════════════════

class _FakeNseClient:
    def __init__(self, responses):
        self._q = list(responses)
    def get(self, url, **kw):
        r = self._q.pop(0) if len(self._q) > 1 else self._q[-1]
        return r


class TestFetchBhavDayParsed:
    def _date(self):
        from datetime import date
        return date(2026, 8, 18)

    def test_returns_parsed_dict_on_success(self):
        d = self._date()
        resp = _FakeResp(200, _FULL_CSV)
        out = bh._fetch_bhav_day_parsed(_FakeNseClient([resp]), d)
        assert out is not None
        assert "RELIANCE" in out

    def test_caches_result(self):
        d = self._date()
        resp = _FakeResp(200, _FULL_CSV)
        bh._fetch_bhav_day_parsed(_FakeNseClient([resp]), d)
        # Second call — client raises if called (cache should answer)
        class _Bomb:
            def get(self, *a, **kw): raise AssertionError("should use cache")
        assert bh._fetch_bhav_day_parsed(_Bomb(), d) is not None

    def test_returns_none_when_all_urls_fail(self):
        d = self._date()
        fail = _FakeResp(404, "")
        urls = bh._bhav_urls_for_date(d)
        out = bh._fetch_bhav_day_parsed(_FakeNseClient([fail] * (len(urls) + 1)), d)
        assert out is None

    def test_skips_empty_content(self):
        d = self._date()
        empty = _FakeResp(200, "", content=b"")
        good = _FakeResp(200, _FULL_CSV)
        out = bh._fetch_bhav_day_parsed(_FakeNseClient([empty, good]), d)
        assert out is not None

    def test_skips_interstitial_html(self):
        d = self._date()
        html_resp = _FakeResp(200, "<HTML>Will be right back</HTML>")
        good = _FakeResp(200, _FULL_CSV)
        out = bh._fetch_bhav_day_parsed(_FakeNseClient([html_resp, good]), d)
        assert out is not None

    def test_skips_json_content_type(self):
        d = self._date()
        json_resp = _FakeResp(200, '{"x":1}',
                              headers={"content-type": "application/json"})
        good = _FakeResp(200, _FULL_CSV)
        out = bh._fetch_bhav_day_parsed(_FakeNseClient([json_resp, good]), d)
        assert out is not None

    def test_handles_zip_content(self):
        d = self._date()
        zip_bytes = _zip_of_csv(_FULL_CSV)
        resp = _FakeResp(200, "", content=zip_bytes,
                         headers={"content-type": "application/zip"})
        # Override URL list so first URL looks like .zip
        import bhavcopy as _bh
        original = _bh._bhav_urls_for_date
        _bh._bhav_urls_for_date = lambda dt: [f"https://example.com/bhav_{dt}.zip"]
        try:
            out = _bh._fetch_bhav_day_parsed(_FakeNseClient([resp]), d)
        finally:
            _bh._bhav_urls_for_date = original
        assert out is not None
        assert "RELIANCE" in out

    def test_cache_evicts_oldest_when_full(self):
        from datetime import date
        bh._BHAV_DAY_CACHE_MAX_DATES = 2
        dates = [date(2026, 8, 14), date(2026, 8, 15), date(2026, 8, 18)]
        for d in dates:
            bh._BHAV_DAY_CACHE[d.isoformat()] = {"SYM": {"close": 100}}
        # Manually trigger eviction on next parse
        bh._BHAV_DAY_CACHE.clear()
        for d in dates[:2]:
            bh._BHAV_DAY_CACHE[d.isoformat()] = {"SYM": {"close": 100}}
        # Now fetch a new date — should evict oldest
        resp = _FakeResp(200, _FULL_CSV)
        bh._fetch_bhav_day_parsed(_FakeNseClient([resp]), dates[2])
        assert len(bh._BHAV_DAY_CACHE) <= bh._BHAV_DAY_CACHE_MAX_DATES


# ══════════════════════════════════════════════════════════════════════════════
# delivery_from_quote
# ══════════════════════════════════════════════════════════════════════════════

class TestDeliveryFromQuote:
    def test_returns_delivery_pct_from_field(self, monkeypatch):
        body = {"securityWiseDP": {
            "deliveryToTradedQuantity": "63.5",
            "quantityTraded": 1000000,
            "deliveryQuantity": 635000,
        }}
        import json
        resp = _FakeResp(200, json.dumps(body))
        class _FC:
            def get(self, *a, **kw): return resp
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        result = bh.delivery_from_quote("RELIANCE")
        assert result["delivery_pct"] == 63.5

    def test_computes_pct_from_qty_when_field_absent(self, monkeypatch):
        body = {"securityWiseDP": {
            "quantityTraded": 1000,
            "deliveryQuantity": 500,
        }}
        import json
        resp = _FakeResp(200, json.dumps(body))
        class _FC:
            def get(self, *a, **kw): return resp
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        result = bh.delivery_from_quote("TCS")
        assert result["delivery_pct"] == 50.0

    def test_returns_none_on_non_200(self, monkeypatch):
        class _FC:
            def get(self, *a, **kw): return _FakeResp(404, "")
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        assert bh.delivery_from_quote("X") is None

    def test_returns_none_on_exception(self, monkeypatch):
        class _FC:
            def get(self, *a, **kw): raise RuntimeError("timeout")
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        assert bh.delivery_from_quote("X") is None

    def test_strips_ns_suffix(self, monkeypatch):
        import json
        body = {"securityWiseDP": {"deliveryToTradedQuantity": "40.0"}}
        resp = _FakeResp(200, json.dumps(body))
        class _FC:
            def get(self, url, **kw):
                assert "RELIANCE" in url and ".NS" not in url
                return resp
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        bh.delivery_from_quote("RELIANCE.NS")


# ══════════════════════════════════════════════════════════════════════════════
# delivery_from_bhavcopy / eod_close_from_bhavcopy
# ══════════════════════════════════════════════════════════════════════════════

class TestDeliveryFromBhavcopy:
    def test_returns_row_from_cache(self, monkeypatch):
        from datetime import date
        monkeypatch.setattr(bh, "_candidate_session_dates",
                            lambda n=6: [date(2026, 8, 18)])
        bh._BHAV_DAY_CACHE["2026-08-18"] = {
            "RELIANCE": {"symbol": "RELIANCE", "delivery_pct": 55.0,
                         "close": 2500.0, "source": "nse_bhavcopy"}
        }
        class _FC: pass
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        result = bh.delivery_from_bhavcopy("RELIANCE.NS")
        assert result["delivery_pct"] == 55.0
        assert result["session_date"] == "2026-08-18"

    def test_returns_none_when_symbol_not_in_any_day(self, monkeypatch):
        from datetime import date
        monkeypatch.setattr(bh, "_candidate_session_dates",
                            lambda n=6: [date(2026, 8, 18)])
        bh._BHAV_DAY_CACHE["2026-08-18"] = {"TCS": {"close": 100}}
        class _FC: pass
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        assert bh.delivery_from_bhavcopy("NOTEXIST") is None

    def test_returns_none_on_exception(self, monkeypatch):
        monkeypatch.setattr(bh, "_nse_client",
                            lambda: (_ for _ in ()).throw(RuntimeError("down")))
        assert bh.delivery_from_bhavcopy("X") is None


class TestEodCloseFromBhavcopy:
    def test_returns_close_from_cache(self, monkeypatch):
        from datetime import date
        monkeypatch.setattr(bh, "_candidate_session_dates",
                            lambda n=6: [date(2026, 8, 18)])
        bh._BHAV_DAY_CACHE["2026-08-18"] = {
            "NTPC": {"close": 150.0, "source": "nse_bhavcopy"}
        }
        class _FC: pass
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        assert bh.eod_close_from_bhavcopy("NTPC") == 150.0

    def test_logs_when_not_found(self, monkeypatch, caplog):
        import logging
        from datetime import date
        monkeypatch.setattr(bh, "_candidate_session_dates",
                            lambda n=6: [date(2026, 8, 18)])
        bh._BHAV_DAY_CACHE["2026-08-18"] = {}
        class _FC: pass
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        with caplog.at_level(logging.INFO):
            result = bh.eod_close_from_bhavcopy("MISSING")
        assert result is None
        assert "not found" in caplog.text

    def test_returns_none_on_exception(self, monkeypatch):
        monkeypatch.setattr(bh, "_nse_client",
                            lambda: (_ for _ in ()).throw(RuntimeError("down")))
        assert bh.eod_close_from_bhavcopy("X") is None


# ══════════════════════════════════════════════════════════════════════════════
# delivery_from_nse_cm_series
# ══════════════════════════════════════════════════════════════════════════════

class TestDeliveryFromNseCmSeries:
    def test_returns_delivery_pct(self, monkeypatch):
        import json
        body = {"data": [{"CH_DELIVERY_PERC": "45.5",
                          "CH_TOT_TRADED_QTY": 1000000,
                          "CH_DELIV_QTY": 455000,
                          "CH_TIMESTAMP": "2026-08-18"}]}
        class _FC:
            def get(self, *a, **kw): return _FakeResp(200, json.dumps(body))
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        result = bh.delivery_from_nse_cm_series("RELIANCE")
        assert result["delivery_pct"] == 45.5
        assert result["source"] == "nse_cm_historical"

    def test_returns_none_on_non_200(self, monkeypatch):
        class _FC:
            def get(self, *a, **kw): return _FakeResp(403, "")
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        assert bh.delivery_from_nse_cm_series("X") is None

    def test_returns_none_when_no_rows(self, monkeypatch):
        import json
        class _FC:
            def get(self, *a, **kw): return _FakeResp(200, json.dumps({"data": []}))
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        assert bh.delivery_from_nse_cm_series("X") is None

    def test_returns_none_when_no_delivery_field(self, monkeypatch):
        import json
        body = {"data": [{"CH_TOT_TRADED_QTY": 1000, "CH_TIMESTAMP": "2026-08-18"}]}
        class _FC:
            def get(self, *a, **kw): return _FakeResp(200, json.dumps(body))
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        assert bh.delivery_from_nse_cm_series("X") is None

    def test_returns_none_on_exception(self, monkeypatch):
        class _FC:
            def get(self, *a, **kw): raise RuntimeError("timeout")
        monkeypatch.setattr(bh, "_nse_client", lambda: _FC())
        assert bh.delivery_from_nse_cm_series("X") is None


# ══════════════════════════════════════════════════════════════════════════════
# get_delivery — waterfall
# ══════════════════════════════════════════════════════════════════════════════

class TestGetDelivery:
    def test_returns_first_hit(self, monkeypatch):
        monkeypatch.setattr(bh, "delivery_from_quote",
            lambda sym: {"delivery_pct": 60.0, "symbol": sym, "source": "nse_quote_equity"})
        result = bh.get_delivery("RELIANCE")
        assert result["delivery_pct"] == 60.0
        assert "fetched_at" in result

    def test_falls_through_to_bhavcopy(self, monkeypatch):
        monkeypatch.setattr(bh, "delivery_from_quote", lambda sym: None)
        monkeypatch.setattr(bh, "delivery_from_bhavcopy",
            lambda sym: {"delivery_pct": 55.0, "symbol": sym, "source": "nse_bhavcopy"})
        result = bh.get_delivery("TCS")
        assert result["delivery_pct"] == 55.0

    def test_falls_through_to_cm_series(self, monkeypatch):
        monkeypatch.setattr(bh, "delivery_from_quote", lambda sym: None)
        monkeypatch.setattr(bh, "delivery_from_bhavcopy", lambda sym: None)
        monkeypatch.setattr(bh, "delivery_from_nse_cm_series",
            lambda sym: {"delivery_pct": 50.0, "symbol": sym, "source": "nse_cm_historical"})
        result = bh.get_delivery("WIPRO")
        assert result["delivery_pct"] == 50.0

    def test_returns_neutral_fallback_when_all_fail(self, monkeypatch):
        monkeypatch.setattr(bh, "delivery_from_quote", lambda sym: None)
        monkeypatch.setattr(bh, "delivery_from_bhavcopy", lambda sym: None)
        monkeypatch.setattr(bh, "delivery_from_nse_cm_series", lambda sym: None)
        result = bh.get_delivery("X")
        assert result["delivery_pct"] == 50.0
        assert result["source"] == "fallback_neutral"
        assert "note" in result

    def test_swallows_exception_from_fn(self, monkeypatch):
        monkeypatch.setattr(bh, "delivery_from_quote",
            lambda sym: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(bh, "delivery_from_bhavcopy",
            lambda sym: {"delivery_pct": 44.0, "symbol": sym, "source": "nse_bhavcopy"})
        result = bh.get_delivery("INFY")
        assert result["delivery_pct"] == 44.0

    def test_uppercase_and_strip_ns_suffix(self, monkeypatch):
        seen = []
        monkeypatch.setattr(bh, "delivery_from_quote",
            lambda sym: seen.append(sym) or {"delivery_pct": 1.0, "symbol": sym, "source": "x"})
        bh.get_delivery("reliance.NS")
        assert seen[0] == "RELIANCE"


# ══════════════════════════════════════════════════════════════════════════════
# process_bhavcopy_dataframe — pure-Python list path
# ══════════════════════════════════════════════════════════════════════════════

class TestProcessBhavcopydDataframe:
    def test_list_path_delegates_to_rows(self):
        rows = [{"SYMBOL": "X", "SERIES": "EQ", "CLOSE": "100"}]
        result = bh.process_bhavcopy_dataframe(rows)
        assert result == rows

    def test_none_passthrough(self):
        assert bh.process_bhavcopy_dataframe(None) is None

    def test_object_with_to_dict_is_handled(self):
        class _FakeDf:
            def to_dict(self, orient="records"):
                return [{"SYMBOL": "X", "SERIES": "EQ", "CLOSE": "100"}]
        result = bh.process_bhavcopy_dataframe(_FakeDf())
        assert result is not None
