"""
E325 - place_order / place_forever_order fail closed on NaN / inf / non-positive numbers.

`not trig or trig <= 0` lets NaN and inf through (NaN is truthy and `nan <= 0` is False), so a NaN stop
trigger was sent to the broker; place_order also never validated `quantity` (place_forever_order did).
Existing behaviour that stays: a LIMIT price of 0 is not rejected (pinned by test_dhan_client.py).

Run from services/real-trade-service:
    python3 -m pytest tests/test_e325_place_order_numeric_guard.py -q
"""
from __future__ import annotations

import pytest

from execution import dhan_client as dc

NAN = float("nan")
INF = float("inf")


class _Sdk:
    def __init__(self):
        self.calls = 0
        self.kwargs = None

    def place_order(self, **kw):
        self.calls += 1
        self.kwargs = kw
        return {"status": "success", "data": {"orderId": "OID1"}}

    def get_order_list(self):
        return {"status": "success", "data": []}


@pytest.fixture()
def sdk(monkeypatch):
    c = _Sdk()
    monkeypatch.setattr(dc, "_get_sdk_client", lambda db: c)
    return c


def place(**over):
    kw = dict(is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT, transaction_type="SELL",
              quantity=10, order_type="MARKET", price=0)
    kw.update(over)
    return dc.place_order(None, **kw)


@pytest.mark.parametrize("q", [0, -5, NAN, INF, 2.5, "abc", None])
def test_bad_quantity_is_refused_before_the_sdk(sdk, q):
    with pytest.raises(ValueError):
        place(quantity=q)
    assert sdk.calls == 0


@pytest.mark.parametrize("q", [1, 10, 10.0, "10"])
def test_good_quantity_still_places(sdk, q):
    assert place(quantity=q) == {"orderId": "OID1"}
    assert sdk.calls == 1


@pytest.mark.parametrize("trig", [NAN, INF, -INF, 0, -1.0, "abc"])
@pytest.mark.parametrize("otype", ["STOP_LOSS_MARKET", "STOP_LOSS"])
def test_bad_trigger_is_refused_before_the_sdk(sdk, trig, otype):
    with pytest.raises(ValueError):
        place(order_type=otype, price=99.0 if otype == "STOP_LOSS" else 0, trigger_price=trig)
    assert sdk.calls == 0


@pytest.mark.parametrize("px", [NAN, INF, -1.0, "abc"])
def test_bad_limit_or_stop_price_is_refused(sdk, px):
    with pytest.raises(ValueError):
        place(order_type="LIMIT", price=px)
    with pytest.raises(ValueError):
        place(order_type="STOP_LOSS", price=px, trigger_price=100.0)
    assert sdk.calls == 0


def test_limit_price_zero_is_still_allowed_as_before(sdk):
    place(order_type="LIMIT", price=0)
    assert sdk.kwargs["price"] == 0


def test_good_stop_order_still_places_with_its_trigger(sdk):
    place(order_type="STOP_LOSS_MARKET", price=0, trigger_price=250.0)
    assert sdk.calls == 1 and sdk.kwargs["trigger_price"] == 250.0


def test_market_order_with_nan_price_is_still_forced_to_zero(sdk):
    place(order_type="MARKET", price=NAN)
    assert sdk.kwargs["price"] == 0.0


@pytest.mark.parametrize("trig", [NAN, INF, 0, -2.0])
def test_forever_order_refuses_a_bad_trigger(monkeypatch, trig):
    monkeypatch.setattr(dc, "_forever_headers_and_client", lambda db: (_ for _ in ()).throw(AssertionError("must not reach REST")))
    with pytest.raises((ValueError, AssertionError)) as ei:
        dc.place_forever_order(None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                               transaction_type="SELL", quantity=5, price=0, trigger_price=trig)
    assert isinstance(ei.value, ValueError)


@pytest.mark.parametrize("px", [NAN, INF])
def test_forever_order_refuses_a_bad_price(monkeypatch, px):
    monkeypatch.setattr(dc, "_forever_headers_and_client", lambda db: (_ for _ in ()).throw(AssertionError("must not reach REST")))
    with pytest.raises(ValueError):
        dc.place_forever_order(None, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                               transaction_type="SELL", quantity=5, price=px, trigger_price=100.0)
