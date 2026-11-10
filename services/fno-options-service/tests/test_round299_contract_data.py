"""Round 299: pick_contract never treats a missing volume or open interest as a liquid strike (absent data is never good news)."""
from __future__ import annotations

import copy
from datetime import datetime

from fno import signals
from fno.provider import IST
from helpers import make_chain

NOW = datetime(2026, 11, 10, 10, 30, tzinfo=IST)


def _chain(**blank):
    ch = copy.deepcopy(make_chain())
    for r in ch.rows:
        for s in (r.ce, r.pe):
            if s:
                for k, v in blank.items():
                    setattr(s, k, v)
    return ch


def test_the_normal_chain_still_picks_a_contract():
    assert signals.pick_contract(_chain(), "UP", NOW, lot_size=65)["ok"]


def test_missing_volume_is_rejected_with_a_reason_on_every_strike():
    r = signals.pick_contract(_chain(volume=None), "UP", NOW, lot_size=65)
    assert not r["ok"] and r["considered"] and all(c["reject"] == "no volume data" for c in r["considered"])


def test_missing_open_interest_is_rejected_with_a_reason_on_every_strike():
    r = signals.pick_contract(_chain(oi=None), "DOWN", NOW, lot_size=65)
    assert not r["ok"] and all(c["reject"] == "no open-interest data" for c in r["considered"])


def test_zero_volume_and_zero_oi_are_still_rejected_by_the_minimums():
    assert not signals.pick_contract(_chain(volume=0.0), "UP", NOW, lot_size=65)["ok"]
    assert not signals.pick_contract(_chain(oi=0.0), "UP", NOW, lot_size=65)["ok"]


def test_one_strike_with_no_volume_does_not_stop_the_others():
    ch = _chain()
    i = ch.atm_index()
    ch.rows[i].ce.volume = None
    r = signals.pick_contract(ch, "UP", NOW, lot_size=65)
    assert r["ok"] and r["contract"].strike != ch.rows[i].strike
    assert [c for c in r["considered"] if c["strike"] == ch.rows[i].strike][0]["reject"] == "no volume data"
