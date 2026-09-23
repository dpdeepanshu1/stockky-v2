"""
tests/test_dhan_client.py

100%-coverage-plan Phase 1 #5: execution/dhan_client.py -- 28% -> target 70%+.
Previously 0% direct coverage on this file specifically (no test_dhan_client.py
existed at all). Two things this session closes:

  1. The `is_*_error` classifier table. These are pure string-matching
     functions that every exit.py/entry.py/manual_engine.py branch depends on
     to route a Dhan rejection to the right handling -- a classifier
     regression here silently breaks a downstream branch without that
     branch's own tests ever catching it (they only test "given a matched
     error", never the matching itself). Error strings below are pulled
     directly from the real rejection text logged in this module's own
     session comments (sessions 21, 38-41, 50-58) rather than invented.

  2. place_order()'s two hardening layers from session40 (DATAMATICS
     position-81 incident): order_type validated against Dhan's documented
     enum before the SDK ever sees it, and order_type=MARKET unconditionally
     forcing price=0 regardless of what the caller passed -- plus the
     post-placement broker-order-type-mismatch verification (and its
     2026-09-17 MARKET-echoed-as-LIMIT false-alarm fix).

round_to_tick/is_valid_tick_price/tick_size_for_price (the NSE price-band
tick-size logic entry_engine.entry and manual_engine.py both depend on for
a valid LIMIT price) are covered too since place_order's LIMIT branch
exercises them directly.

Run from services/real-trade-service:
    python -m pytest tests/test_dhan_client.py -q
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from execution import dhan_client as dc


# ── Classifier table ─────────────────────────────────────────────────────────
# Each row: (classifier function, [real rejection strings it must match],
#            [strings from OTHER classifiers/generic errors it must NOT match]).
# Pulled from the exact wording documented in dhan_client.py's own session
# comments so this table is a regression guard on real, previously-seen
# Dhan text, not synthetic strings that happen to contain the marker.

_CASES = [
    (
        dc.is_auth_error,
        [
            "DH-901: Invalid access token",
            "DH-902: invalid token supplied",
            "DH-905: Token has expired",
            "401 Unauthorized",
            "Authentication failed for this request",
        ],
        ["RMS:123:You have insufficient funds. Please add Rs.500 to trade."],
    ),
    (
        dc.is_invalid_ip_error,
        [
            "DH-907: Invalid IP",
            "DH-908: IP not whitelisted",
            "Unauthorized IP address not allowed",
        ],
        ["DH-901: Invalid access token"],
    ),
    (
        dc.is_cdsl_edis_error,
        [
            "Dhan API error: Validate Qty from CDSL",
            "eDIS authorization pending for this holding",
            "TPIN verification required",
            "Insufficient Holding Quantity",
            "Scrip limit insufficient",
        ],
        ["RMS:456:Order rejected as this stock is not allowed to be traded in Intraday."],
    ),
    (
        dc.is_insufficient_funds_error,
        [
            "RMS:123:You have insufficient funds. Please add Rs.500 to trade.",
            "Insufficient fund in trading account",
        ],
        ["Insufficient Holding Quantity"],  # CDSL wording, not funds wording
    ),
    (
        dc.is_intraday_cutoff_error,
        [
            "Intraday orders cannot be placed at this time",
            "Market is closed for intraday square off time",
        ],
        ["RMS:456:Order rejected as this stock is not allowed to be traded in Intraday."],
    ),
    (
        dc.is_security_intraday_restricted_error,
        [
            "RMS:456:Order rejected as this stock is not allowed to be traded in Intraday.",
            "This security is not allowed to trade in Intraday",
        ],
        ["Intraday orders cannot be placed at this time"],
    ),
    (
        dc.is_circuit_limit_error,
        [
            "RMS:789:Rate Not Within Ckt Limit 395.25 To 592.85",
            "Order price not within circuit limit",
        ],
        ["EXCH:16387:Security is not allowed to trade in this market."],
    ),
    (
        dc.is_oversell_error,
        [
            "RMS:321:You are trying to sell more than the quantity you currently hold.",
            "Cannot sell more than available quantity",
        ],
        ["RMS:789:Rate Not Within Ckt Limit 395.25 To 592.85"],
    ),
    (
        dc.is_exchange_not_allowed_error,
        [
            "EXCH:16387:Security is not allowed to trade in this market.",
            "Scrip is not tradeable currently",
        ],
        ["RMS:456:Order rejected as this stock is not allowed to be traded in Intraday."],
    ),
]


class TestClassifierTable:
    @pytest.mark.parametrize(
        "fn,positives,negatives", _CASES,
        ids=[c[0].__name__ for c in _CASES],
    )
    def test_classifier_matches_real_positives_and_rejects_real_negatives(self, fn, positives, negatives):
        for msg in positives:
            assert fn(msg) is True, f"{fn.__name__} should match: {msg!r}"
        for msg in negatives:
            assert fn(msg) is False, f"{fn.__name__} should NOT match: {msg!r}"

    def test_all_classifiers_are_case_insensitive(self):
        for fn, positives, _ in _CASES:
            assert fn(positives[0].upper()) is True, f"{fn.__name__} should be case-insensitive"

    def test_all_classifiers_handle_none_and_empty_message(self):
        for fn, _, _ in _CASES:
            assert fn(None) is False
            assert fn("") is False

    def test_generic_unrecognized_error_matches_no_classifier(self):
        """A message that doesn't match any known pattern (the generic
        catch-all branch every classifier's caller falls back to) must not
        be accidentally swept up by one of these -- that would silently
        misroute a genuinely-unclassified rejection."""
        generic = "RMS:999:Order rejected due to an internal system error, please retry."
        for fn, _, _ in _CASES:
            assert fn(generic) is False, f"{fn.__name__} incorrectly matched a generic error"

    def test_cdsl_and_intraday_restricted_are_mutually_exclusive_on_real_pairs(self):
        """Regression guard for the session21e distinction the module
        docstring calls out explicitly: CDSL/eDIS (settlement-timing) and
        security-intraday-restricted (permanent per-security) must never
        both fire on the same message, or exit.py's branch dispatch (which
        checks these in sequence) could take the wrong path."""
        for msg in ["Dhan API error: Validate Qty from CDSL", "Insufficient Holding Quantity"]:
            assert dc.is_security_intraday_restricted_error(msg) is False
        for msg in ["RMS:456:Order rejected as this stock is not allowed to be traded in Intraday."]:
            assert dc.is_cdsl_edis_error(msg) is False


# ── Tick-size / rounding ─────────────────────────────────────────────────────

class TestTickSizeBands:
    @pytest.mark.parametrize("price,expected_tick", [
        (100.0, 0.01),   # < 250
        (249.99, 0.01),
        (250.0, 0.05),   # [250, 1000)
        (999.99, 0.05),
        (1000.0, 0.10),  # [1000, 5000)
        (5000.0, 0.50),  # [5000, 10000)
        (10000.0, 1.00), # [10000, 20000)
        (20000.0, 5.00), # >= 20000
    ])
    def test_tick_size_for_price_bands(self, price, expected_tick):
        assert dc.tick_size_for_price(price) == expected_tick

    def test_tick_size_for_price_nonpositive_or_invalid_falls_back_to_flat_default(self):
        assert dc.tick_size_for_price(0) == dc.TICK_SIZE
        assert dc.tick_size_for_price(-5) == dc.TICK_SIZE
        assert dc.tick_size_for_price("not-a-number") == dc.TICK_SIZE

    def test_round_to_tick_rounds_to_band_tick_exactly(self):
        # 456.783 sits in the [250,1000) band -> tick 0.05
        assert dc.round_to_tick(456.783) == 456.8
        # 105.567 sits in the <250 band -> tick 0.01
        assert dc.round_to_tick(105.567) == 105.57

    def test_round_to_tick_leaves_nonpositive_price_unchanged(self):
        assert dc.round_to_tick(0) == 0
        assert dc.round_to_tick(-10) == -10

    def test_round_to_tick_invalid_input_returns_input_unchanged(self):
        assert dc.round_to_tick("garbage") == "garbage"

    def test_is_valid_tick_price_true_for_exact_multiple(self):
        assert dc.is_valid_tick_price(456.80) is True  # 0.05 band, exact

    def test_is_valid_tick_price_false_for_off_tick_price(self):
        assert dc.is_valid_tick_price(456.783) is False

    def test_is_valid_tick_price_nonpositive_treated_as_trivially_valid(self):
        assert dc.is_valid_tick_price(0) is True

    def test_is_valid_tick_price_invalid_input_returns_false(self):
        assert dc.is_valid_tick_price("garbage") is False


# ── place_order ───────────────────────────────────────────────────────────────

class _FakeSDKClient:
    def __init__(self, place_response=None, order_list=None):
        self._place_response = place_response or {"status": "success", "data": {"orderId": "OID1"}}
        self._order_list = order_list if order_list is not None else []
        self.place_order_kwargs = None

    def place_order(self, **kwargs):
        self.place_order_kwargs = kwargs
        return self._place_response

    def get_order_list(self):
        return {"status": "success", "data": self._order_list}


@pytest.fixture()
def fake_client(monkeypatch):
    client = _FakeSDKClient()
    monkeypatch.setattr(dc, "_get_sdk_client", lambda db: client)
    return client


class TestPlaceOrderGuards:
    def test_not_armed_raises_without_touching_sdk(self, monkeypatch):
        monkeypatch.setattr(dc, "_get_sdk_client", lambda db: (_ for _ in ()).throw(AssertionError("should not be called")))
        with pytest.raises(dc.DhanNotArmedError):
            dc.place_order(
                None, is_armed=False, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=1, order_type="MARKET", price=0,
            )

    def test_unrecognized_order_type_rejected_before_reaching_sdk(self, monkeypatch):
        monkeypatch.setattr(dc, "_get_sdk_client", lambda db: (_ for _ in ()).throw(AssertionError("should not be called")))
        with pytest.raises(ValueError, match="unrecognized order_type"):
            dc.place_order(
                None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=1, order_type="STOP", price=0,
            )

    def test_market_order_forces_price_to_zero_even_if_caller_passed_nonzero(self, fake_client):
        """session40 regression: a stale/nonzero price argument reaching
        place_order for a MARKET order must never leak through as a real
        limit price."""
        dc.place_order(
            None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=10, order_type="MARKET", price=456.75,
        )
        assert fake_client.place_order_kwargs["price"] == 0.0
        assert fake_client.place_order_kwargs["order_type"] == "MARKET"

    def test_limit_order_price_rounded_to_valid_tick_before_sdk_call(self, fake_client):
        dc.place_order(
            None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=10, order_type="LIMIT", price=456.783,
        )
        assert fake_client.place_order_kwargs["price"] == 456.80
        assert fake_client.place_order_kwargs["order_type"] == "LIMIT"

    def test_limit_order_zero_price_left_alone_not_rounded_or_rejected(self, fake_client):
        # price=0/falsy skips the LIMIT-rounding branch entirely (elif price:)
        dc.place_order(
            None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=10, order_type="LIMIT", price=0,
        )
        assert fake_client.place_order_kwargs["price"] == 0

    def test_returns_extracted_data_from_sdk_response(self, fake_client):
        result = dc.place_order(
            None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=10, order_type="MARKET", price=0,
        )
        assert result == {"orderId": "OID1"}


class TestPlaceOrderBrokerVerification:
    """The post-placement get_order_list() cross-check (session40, part 3)
    -- best-effort, must never raise, and must not false-alarm on Dhan's
    documented MARKET-echoed-as-LIMIT behavior (2026-09-17 fix)."""

    def test_market_order_echoed_as_limit_by_broker_is_not_flagged_as_mismatch(self, monkeypatch, caplog):
        client = _FakeSDKClient(
            place_response={"status": "success", "data": {"orderId": "OID1"}},
            order_list=[{"orderId": "OID1", "orderType": "LIMIT", "price": 250.0}],
        )
        monkeypatch.setattr(dc, "_get_sdk_client", lambda db: client)
        with caplog.at_level(logging.CRITICAL, logger="real-trade-dhan-client"):
            dc.place_order(
                None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=10, order_type="MARKET", price=0,
            )
        assert not any("MISMATCH" in rec.message for rec in caplog.records)

    def test_genuine_order_type_mismatch_logs_critical_but_does_not_raise(self, monkeypatch, caplog):
        client = _FakeSDKClient(
            place_response={"status": "success", "data": {"orderId": "OID1"}},
            order_list=[{"orderId": "OID1", "orderType": "STOP_LOSS_MARKET", "price": 0}],
        )
        monkeypatch.setattr(dc, "_get_sdk_client", lambda db: client)
        with caplog.at_level(logging.CRITICAL, logger="real-trade-dhan-client"):
            result = dc.place_order(
                None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=10, order_type="MARKET", price=0,
            )
        assert result == {"orderId": "OID1"}  # placement result still returned
        assert any("MISMATCH" in rec.message for rec in caplog.records)

    def test_verification_failure_is_swallowed_and_does_not_break_placement(self, monkeypatch):
        client = _FakeSDKClient(place_response={"status": "success", "data": {"orderId": "OID1"}})

        def _broken_order_list():
            raise RuntimeError("network blip")
        client.get_order_list = _broken_order_list
        monkeypatch.setattr(dc, "_get_sdk_client", lambda db: client)

        result = dc.place_order(
            None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=10, order_type="MARKET", price=0,
        )
        assert result == {"orderId": "OID1"}

    def test_no_verification_attempted_when_sdk_returned_no_order_id(self, monkeypatch):
        client = _FakeSDKClient(place_response={"status": "success", "data": {}})

        def _should_not_be_called():
            raise AssertionError("get_order_list should not be called with no placed_order_id")
        client.get_order_list = _should_not_be_called
        monkeypatch.setattr(dc, "_get_sdk_client", lambda db: client)

        result = dc.place_order(
            None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=10, order_type="MARKET", price=0,
        )
        assert result == {}
