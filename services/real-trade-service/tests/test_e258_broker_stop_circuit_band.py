"""E258 (29 Oct 2026, ITC): the resting broker stop must sit inside the exchange circuit band.

A trigger below the lower circuit is refused by Dhan RMS ("Rate Not Within Ckt Limit 257.80 To 273.70"). Before: no clamp, no
backoff - the same doomed order was re-sent every sync pass. Now the trigger is raised to the lower circuit + 1 tick (band from
the live tick, or learned from the rejection text) and a repeat refusal backs the symbol off.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from execution import broker_stop as B
from test_group322_broker_stop import pos, rig  # noqa: F401  (rig is a fixture)

ITC_REJECT = "RMS:23126102968907:Rate Not Within Ckt Limit 257.80 To 273.70"


def _sync(db, positions, ticks, now):
    return asyncio.run(B.sync_stops(db, "REAL", positions, ticks, now_mono=now))


def test_trigger_for_raises_to_lower_circuit_plus_one_tick():
    # stop 257.50 less the 0.5 % buffer = 256.21, below the 257.80 band floor
    assert B.trigger_for(257.50, 265.0, 257.80) == 257.85
    assert B.trigger_for(257.50, 265.0) == 256.2                       # no band known -> unchanged behaviour
    assert B.trigger_for(262.00, 265.0, 257.80) == 260.7               # already inside the band -> unchanged
    assert B.trigger_for(257.50, 257.85, 257.80) is None               # on the lower circuit: nothing can rest below the market


def test_parse_circuit_band():
    assert B.parse_circuit_band(ITC_REJECT) == (257.80, 273.70)
    assert B.parse_circuit_band("Place order within 78.95 to 113.60.") == (78.95, 113.60)
    assert B.parse_circuit_band("eDIS needed") is None


def test_sync_places_inside_the_band_when_the_tick_knows_it(rig):  # noqa: F811
    db, st = rig
    p = pos(db, "ITC", qty=1, stop=257.50)
    tally = _sync(db, [p], {"ITC": SimpleNamespace(price=265.0, lower_circuit=257.80, upper_circuit=273.70)}, 1000.0)
    assert tally["placed"] == 1 and st.placed[0]["trigger_price"] == pytest.approx(257.85)


def test_rejection_teaches_the_band_and_the_next_pass_succeeds(rig):  # noqa: F811
    db, st = rig
    p = pos(db, "ITC", qty=1, stop=257.50)
    ticks = {"ITC": SimpleNamespace(price=265.0)}                      # no circuit data on the tick
    st.place_error = RuntimeError(ITC_REJECT)
    assert _sync(db, [p], ticks, 1000.0)["failed"] == 1
    assert B._band["ITC"] == (257.80, 273.70)
    assert B.edis_backoff_left("ITC", 1000.0) == 0                     # first refusal: no backoff, the corrected trigger is tried next
    st.place_error = None
    assert _sync(db, [p], ticks, 2000.0)["placed"] == 1
    assert st.placed[-1]["trigger_price"] == pytest.approx(257.85)


def test_a_repeat_refusal_backs_the_symbol_off(rig):  # noqa: F811
    db, st = rig
    p = pos(db, "ITC", qty=1, stop=257.50)
    ticks = {"ITC": SimpleNamespace(price=265.0)}
    st.place_error = RuntimeError(ITC_REJECT)
    _sync(db, [p], ticks, 1000.0)                                      # learns the band
    _sync(db, [p], ticks, 2000.0)                                      # refused again even inside the band
    assert B.edis_backoff_left("ITC", 2000.0) > 0
    assert _sync(db, [p], ticks, 2500.0)["backoff"] == 1               # inside the 900 s window: no new Dhan call
