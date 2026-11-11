"""E449 (round 428): an inf price (the float inf, "inf", "1e999") is not a price. It survived data_feed._coerce_price (only NaN was refused), so the
Health Audit counted the row as priced and Repair never fixed it; refill_additional's priced-symbol test was a bare truthiness check."""
import math

import pytest

import data_feed
import main as gw
import refill_additional as ra


@pytest.mark.parametrize("v", [float("inf"), "inf", "Infinity", "1e999", "1,e999", float("nan"), -5, "-1e999", 0, "", None, "abc"])
def test_coerce_price_refuses_non_finite_and_non_positive(v):
    assert data_feed._coerce_price(v) == 0.0


@pytest.mark.parametrize("v,exp", [(12.5, 12.5), ("1,234.50", 1234.5), (7, 7.0)])
def test_coerce_price_still_parses_real_prices(v, exp):
    assert data_feed._coerce_price(v) == exp


def test_payload_price_skips_an_inf_field_and_uses_the_next_real_one():
    assert data_feed._payload_price({"price": "inf", "close": 101.5}) == 101.5
    assert data_feed._payload_price({"price": float("inf")}) == 0.0


def test_resolved_price_is_never_inf():
    px = gw._feed_resolved_price({"symbol": "AAA", "price": float("inf")})
    assert math.isfinite(px)


def test_audit_reports_an_inf_priced_row_as_missing_price():
    assert "price" in gw._feed_missing_fields({"symbol": "AAA", "price": "inf", "rsi": 50, "pe_ratio": 20, "roce": 15, "sentiment_score": 0.5})


def test_audit_keeps_a_real_price_out_of_the_missing_list():
    assert "price" not in gw._feed_missing_fields({"symbol": "AAA", "price": 250.0, "rsi": 50, "pe_ratio": 20, "roce": 15, "sentiment_score": 0.5})


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def get_symbols_bulk(self, syms):
        return self.rows


def test_priced_symbols_needs_a_finite_positive_price():
    rows = {"GOOD": {"price": 10.0}, "STR": {"last_price": "1,200.5"}, "INF": {"price": float("inf")}, "NAN": {"price": float("nan")},
            "JUNK": {"price": "n/a"}, "ZERO": {"price": 0}, "NONE": {}}
    assert ra._priced_symbols(_Store(rows), list(rows)) == {"GOOD", "STR"}
