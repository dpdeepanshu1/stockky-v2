"""E258 (29 Oct 2026, ITC): the overnight stop must sit inside the exchange circuit band.

ITC: entry ~266.8, stop 4 % below = 256.15, band 257.80 - 273.70 -> Dhan RMS "Rate Not Within Ckt Limit", the stop failed and
the carry fell back to a square-off. The trigger is now raised to the lower circuit + 1 tick (known band) or, when the quote does
not carry the band, once more after Dhan's own rejection text names it.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from execution import dhan_client
from orders import circuit_gate, eod_squareoff

ITC_REJECT = "RMS:23126102968907:Rate Not Within Ckt Limit 257.80 To 273.70"


def test_parse_band_reads_both_dhan_phrasings():
    assert circuit_gate.parse_band(ITC_REJECT) == (257.80, 273.70)
    assert circuit_gate.parse_band("RMS:1:Order rejected, Stock in circuit freeze. Place order within 78.95 to 113.60.") == (78.95, 113.60)
    assert circuit_gate.parse_band("insufficient funds") is None
    assert circuit_gate.parse_band(None) is None
    assert circuit_gate.parse_band("Ckt Limit 300 To 200") is None          # nonsense band


def test_clamp_sell_stop():
    assert circuit_gate.clamp_sell_stop(256.15, 257.80, 0.05, 265.0) == 257.85     # below the band -> lower + 1 tick
    assert circuit_gate.clamp_sell_stop(260.00, 257.80, 0.05, 265.0) == 260.00     # inside the band -> unchanged
    assert circuit_gate.clamp_sell_stop(256.15, None, 0.05, 265.0) == 256.15       # no band known -> no opinion
    assert circuit_gate.clamp_sell_stop(256.15, 257.80, 0.05, 257.85) is None      # on the lower circuit: no stop can rest below the market
    assert circuit_gate.clamp_sell_stop(256.15, 257.80, 0.05, None) == 257.85      # no live price known -> still clamped


def _pos():
    return SimpleNamespace(symbol="ITC", id=7, entry_price=266.8, dhan_security_id="1594", quantity=1, side="LONG")


@pytest.fixture()
def rig(monkeypatch):
    calls = []
    critical = []
    monkeypatch.setattr(eod_squareoff.notifier, "notify_critical", lambda m: critical.append(m))
    return calls, critical, monkeypatch


def _stub_place(calls, monkeypatch, fail_first=None):
    def place(db, **kw):
        calls.append(kw["trigger_price"])
        if fail_first and len(calls) == 1:
            raise RuntimeError(fail_first)
        return {"orderId": "S%d" % len(calls)}
    monkeypatch.setattr(dhan_client, "place_cnc_stop_loss_market", place)


def test_known_band_raises_the_trigger_before_placing(rig):
    calls, critical, mp = rig
    mp.setattr(circuit_gate, "band_for", lambda s: (257.80, 273.70, 265.0))
    _stub_place(calls, mp)
    assert eod_squareoff._place_overnight_stop(None, _pos(), 4.0) == "S1"
    assert calls == [257.85]                      # 266.8 * 0.96 = 256.13 is below the band
    assert critical == []


def test_unknown_band_retries_once_from_dhans_rejection_text(rig):
    calls, critical, mp = rig
    mp.setattr(circuit_gate, "band_for", lambda s: (None, None, None))
    _stub_place(calls, mp, fail_first=ITC_REJECT)
    assert eod_squareoff._place_overnight_stop(None, _pos(), 4.0) == "S2"
    assert calls == [pytest.approx(256.128), 257.85]      # the first send is the raw 4 % level (rounded to a tick inside dhan_client)
    assert critical == []


def test_stock_on_its_lower_circuit_gets_no_stop_and_no_call(rig):
    calls, critical, mp = rig
    mp.setattr(circuit_gate, "band_for", lambda s: (257.80, 273.70, 257.85))
    _stub_place(calls, mp)
    assert eod_squareoff._place_overnight_stop(None, _pos(), 4.0) is None
    assert calls == []


def test_other_errors_are_not_retried_and_still_alert(rig):
    calls, critical, mp = rig
    mp.setattr(circuit_gate, "band_for", lambda s: (None, None, None))
    _stub_place(calls, mp, fail_first="RMS:1:Insufficient Holding Quantity")
    assert eod_squareoff._place_overnight_stop(None, _pos(), 4.0) is None
    assert len(calls) == 1
    assert critical and "OVERNIGHT STOP PLACEMENT FAILED" in critical[0]


def test_stop_already_inside_the_band_is_untouched(rig):
    calls, critical, mp = rig
    mp.setattr(circuit_gate, "band_for", lambda s: (230.0, 300.0, 266.0))
    _stub_place(calls, mp)
    assert eod_squareoff._place_overnight_stop(None, _pos(), 4.0) == "S1"
    assert calls == [pytest.approx(256.128)]


def test_quote_failure_fails_open(rig, monkeypatch):
    from orders import depth_gate
    monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: (_ for _ in ()).throw(RuntimeError("down")))
    assert circuit_gate.band_for("ITC") == (None, None, None)
