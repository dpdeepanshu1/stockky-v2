"""group 289: every market-data quote payload carries as_of (tz-aware UTC) and age_s next to source,
and both are recomputed when the response is built (a cached row must not keep the age it was stored with).

Run from services/market-data-service:
    python3 -m pytest tests/test_group289_quote_as_of.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import main as m

client = TestClient(m.app, raise_server_exceptions=False)


def _naive_utc(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).replace(tzinfo=None).isoformat()


class TestAsOfAge:
    def test_padded_quote_has_as_of_and_age(self):
        out = m._pad_quote_response("TCS", {"price": 100.0, "source": "dhan", "fetched_at": _naive_utc(5)})
        assert out["as_of"].endswith("+00:00")
        assert 4.5 <= out["age_s"] <= 6.5
        assert out["source"] == "dhan"

    def test_missing_fetched_at_is_stamped_now(self):
        out = m._pad_quote_response("TCS", {"price": 100.0})
        assert out["as_of"] is not None and out["age_s"] < 2

    @pytest.mark.parametrize("bad", ["yesterday-ish", None, "", "   ", 12345.5, object()])
    def test_unparseable_fetched_at_gives_unknown_not_fresh(self, bad):
        assert m._quote_as_of_age(bad) == (None, None)

    def test_future_timestamp_never_gives_negative_age(self):
        assert m._quote_as_of_age(_naive_utc(-60))[1] == 0.0

    def test_z_suffix_and_offset_forms_are_read(self):
        now = datetime.now(timezone.utc)
        assert m._quote_as_of_age((now - timedelta(seconds=3)).isoformat().replace("+00:00", "Z"))[1] < 5
        ist = (now - timedelta(seconds=10)).astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
        as_of, age = m._quote_as_of_age(ist)
        assert 9.5 <= age <= 12 and as_of.endswith("+00:00")      # normalised to UTC whatever the input offset

    def test_naive_input_is_read_as_utc_not_local_time(self):
        # a machine in IST must not shift a naive-UTC stamp by 5.5 h
        import time
        if not hasattr(time, "tzset"):      # pragma: no cover  (non-POSIX)
            pytest.skip("tzset unavailable")
        before = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Kolkata"
        time.tzset()
        try:
            assert 4 <= m._quote_as_of_age(_naive_utc(5))[1] <= 7
        finally:
            if before is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = before
            time.tzset()


class TestRestamp:
    def test_overwrites_an_age_stored_in_the_cache(self):
        row = {"price": 1.0, "fetched_at": _naive_utc(120), "as_of": "stale", "age_s": 0.2}
        m._restamp_quote(row)
        assert 119 <= row["age_s"] <= 122 and row["as_of"] != "stale"

    def test_unreadable_time_clears_a_stale_age(self):
        row = {"price": 1.0, "fetched_at": "??", "as_of": "x", "age_s": 0.2}
        m._restamp_quote(row)
        assert row["as_of"] is None and row["age_s"] is None

    def test_row_with_no_timestamp_and_no_derived_fields_is_returned_unchanged(self):
        row = {"symbol": "Y"}
        assert m._restamp_quote(row) == {"symbol": "Y"}

    def test_row_with_a_stale_age_but_no_timestamp_is_cleared_not_trusted(self):
        row = {"symbol": "Y", "age_s": 0.1, "as_of": "old"}
        m._restamp_quote(row)
        assert row["age_s"] is None and row["as_of"] is None

    def test_leaves_fetched_at_and_non_dicts_alone(self):
        ts = _naive_utc(30)
        row = {"fetched_at": ts}
        assert m._restamp_quote(row)["fetched_at"] == ts
        assert m._restamp_quote(None) is None
        assert m._restamp_quote("x") == "x"


class TestPayloads:
    def test_failed_payload_also_carries_the_fields(self):
        out = m._failed_quote_payload("ZZZ")
        assert out["as_of"] is not None and out["age_s"] is not None and out["price"] is None

    def test_response_model_keeps_the_fields(self):
        q = m.QuoteResponse(**m._pad_quote_response("TCS", {"price": 1.0, "fetched_at": _naive_utc(1)}))
        assert q.as_of and q.age_s is not None

    def test_response_model_defaults_to_unknown(self):
        q = m.QuoteResponse(symbol="TCS")
        assert q.as_of is None and q.age_s is None

    def test_existing_fields_are_untouched(self):
        out = m._pad_quote_response("TCS", {"price": 101.5, "previous_close": 100.0, "volume": 10, "source": "x"})
        assert out["price"] == 101.5 and out["previous_close"] == 100.0 and out["volume"] == 10 and out["symbol"] == "TCS"


class TestWrappers:
    def test_get_quote_wrapper_restamps_a_cached_row(self, monkeypatch):
        stale = {"symbol": "TCS", "price": 1.0, "source": "cache", "fetched_at": _naive_utc(90), "as_of": "old", "age_s": 0.1}
        monkeypatch.setattr(m, "_get_quote_inner", lambda symbol: dict(stale))
        out = m.get_quote("TCS")
        assert 89 <= out["age_s"] <= 92 and out["as_of"] != "old"

    def test_get_quote_wrapper_does_not_mutate_the_cached_dict(self, monkeypatch):
        cached = {"symbol": "TCS", "price": 1.0, "source": "cache", "fetched_at": _naive_utc(90), "age_s": 0.1}
        monkeypatch.setattr(m, "_get_quote_inner", lambda symbol: cached)
        m.get_quote("TCS")
        assert cached["age_s"] == 0.1     # the shared cache object keeps what was stored

    def test_bulk_wrapper_restamps_every_row(self, monkeypatch):
        rows = [{"symbol": "A", "price": 1.0, "fetched_at": _naive_utc(40), "age_s": 0.0},
                {"symbol": "B", "price": 2.0, "fetched_at": _naive_utc(2), "age_s": 99.0}]
        monkeypatch.setattr(m, "_get_quotes_bulk_core", lambda req, stale_out: {"ok": True, "quotes": [dict(r) for r in rows]})
        out = m.get_quotes_bulk(m.BulkQuoteRequest(symbols=["A", "B"]))
        ages = {q["symbol"]: q["age_s"] for q in out["quotes"]}
        assert 39 <= ages["A"] <= 42 and ages["B"] < 5

    def test_bulk_wrapper_tolerates_error_shapes(self, monkeypatch):
        for shape in ({"ok": False, "error": "No symbols", "quotes": []}, {"ok": True}, {"ok": True, "quotes": None},
                      {"ok": True, "quotes": [None, "junk", {"symbol": "A", "fetched_at": _naive_utc(1)}]}):
            monkeypatch.setattr(m, "_get_quotes_bulk_core", lambda req, stale_out, s=shape: dict(s))
            out = m.get_quotes_bulk(m.BulkQuoteRequest(symbols=["A"]))
            assert isinstance(out, dict) and out.get("ok") in (True, False)

    def test_http_quote_endpoint_returns_the_fields(self, monkeypatch):
        monkeypatch.setattr(m, "_get_quote_inner", lambda symbol: {
            "symbol": "TCS", "price": 100.0, "source": "dhan", "fetched_at": _naive_utc(7)})
        r = client.get("/quote/TCS")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "dhan" and body["as_of"].endswith("+00:00") and 6 <= body["age_s"] <= 9

    def test_http_bulk_endpoint_returns_the_fields(self, monkeypatch):
        monkeypatch.setattr(m, "_get_quotes_bulk_core", lambda req, stale_out: {
            "ok": True, "quotes": [{"symbol": "A", "price": 1.0, "source": "dhan", "fetched_at": _naive_utc(12)}]})
        r = client.post("/quotes/bulk", json={"symbols": ["A"]})
        assert r.status_code == 200
        q = r.json()["quotes"][0]
        assert q["source"] == "dhan" and 11 <= q["age_s"] <= 14
