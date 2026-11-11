"""E424: the `yahoo_missing` placeholder (a symbol the bulk quote call did not return) neither stamps `price_refreshed_at`
(no price was refreshed) nor replaces the `source` label of a row that already has one."""
from __future__ import annotations

import sys

import data_feed as df
from test_data_feed import FakeResp, fake_httpx, _clean_state  # noqa: F401


def _placeholder(monkeypatch):
    body = {"quotes": [{"symbol": "TCS", "price": 100.0, "fetched_at": "T0"}]}
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx(post=lambda url, json=None, timeout=None: FakeResp(200, body)))
    monkeypatch.setenv("MARKET_DATA_URL", "https://mds.example/")
    return df.bulk_yahoo_download_prices(["TCS", "MISSING"])["MISSING"]


def test_the_placeholder_does_not_claim_a_price_refresh(monkeypatch):
    ph = _placeholder(monkeypatch)
    assert ph["source"] == "yahoo_missing" and "price_refreshed_at" not in ph
    assert ph["pe_ratio_seed"] is True and ph["roce_seed"] is True and ph["sentiment_seed"] is True   # seeds unchanged


def test_merging_the_placeholder_keeps_an_existing_source_and_refresh_stamp(monkeypatch):
    existing = {"symbol": "MISSING", "source": "nse_bhavcopy", "price_refreshed_at": "T1", "prev_close": 50.0}
    out = df.merge_feed_payload(existing, _placeholder(monkeypatch))
    assert out["source"] == "nse_bhavcopy" and out["price_refreshed_at"] == "T1" and out["prev_close"] == 50.0
    assert out["pe_ratio"] == 22.5 and out["pe_ratio_seed"] is True        # the seeds still land on a row without them


def test_a_row_without_a_source_takes_the_placeholder_label(monkeypatch):
    out = df.merge_feed_payload({"symbol": "MISSING"}, _placeholder(monkeypatch))
    assert out["source"] == "yahoo_missing" and "price_refreshed_at" not in out


def test_other_sources_still_replace_the_label():
    out = df.merge_feed_payload({"source": "yahoo_missing"}, {"source": "yahoo_bulk"})
    assert out["source"] == "yahoo_bulk"
    out = df.merge_feed_payload({"source": "nse_bhavcopy"}, {"source": "yahoo_bulk"})
    assert out["source"] == "yahoo_bulk"
