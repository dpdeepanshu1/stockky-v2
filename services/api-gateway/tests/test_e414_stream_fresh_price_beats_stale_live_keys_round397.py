"""E414: /scan/stream's per-chunk live price refreshes a feed row's stale last_price / current_price (which E410 made the scan read first)
without adding them to rows that never had them."""
from __future__ import annotations

from test_main_scan_stream_control import _stream, senv, gw  # noqa: F401


def test_stale_last_and_current_price_are_replaced_by_the_fresh_quote(senv):
    senv.feeds = {"AAA": {"prev_close": 100.0, "last_price": 101.0, "current_price": 101.0}}
    senv.prices = {"AAA": 105.5}
    senv.universe = ["AAA"]
    _stream(lite=True)
    row = {c[0]: c for c in senv.lite_calls}["AAA"][1]
    assert row["last_price"] == 105.5 and row["current_price"] == 105.5 and row["prev_close"] == 100.0
    assert gw._feed_row_price(row) == 105.5


def test_rows_without_live_keys_do_not_gain_them(senv):
    senv.feeds = {"AAA": {"prev_close": 100.0}}
    senv.prices = {"AAA": 105.5}
    senv.universe = ["AAA"]
    _stream(lite=True)
    row = {c[0]: c for c in senv.lite_calls}["AAA"][1]
    assert "last_price" not in row and "current_price" not in row and row["close"] == 105.5


def test_a_symbol_without_a_quote_keeps_its_stored_live_price(senv):
    senv.feeds = {"AAA": {"last_price": 101.0}}
    senv.prices = {}
    senv.universe = ["AAA"]
    _stream(lite=True)
    assert {c[0]: c for c in senv.lite_calls}["AAA"][1] == {"last_price": 101.0}
