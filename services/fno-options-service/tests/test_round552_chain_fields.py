"""Round 552 (P3): the Chain tab shows ATM IV, the ATM straddle / expected move and the change in OI. They come from fields the service already sends; this pins them so the page cannot silently lose them."""
from __future__ import annotations

from fno import analytics
from helpers import make_chain


def test_every_strike_sent_to_the_browser_carries_oi_and_prev_oi_and_iv():
    d = make_chain().to_json()
    assert d["strikes"]
    for row in d["strikes"]:
        for side in ("ce", "pe"):
            assert {"oi", "prev_oi", "iv"} <= set(row[side])
            assert row[side]["oi"] is not None and row[side]["prev_oi"] is not None


def test_summary_has_the_atm_numbers_the_page_shows():
    ch = make_chain(spot=24500.0, iv=0.14)
    s = analytics.chain_summary(ch, 3)
    assert {"atm_strike", "atm_iv", "straddle", "expected_move_pct"} <= set(s)
    assert s["atm_strike"] == 24500.0 and 13.0 < s["atm_iv"] < 15.0
    assert s["straddle"] > 0 and abs(s["expected_move_pct"] - s["straddle"] / 24500.0 * 100.0) < 0.01


def test_the_oi_change_the_page_sums_matches_what_the_service_counts_near_the_money():
    ch = make_chain(n_each=3)                                   # only the strikes within 3 of ATM, so both sums cover the same strikes
    s = analytics.chain_summary(ch, 3)
    ce = sum(r.ce.oi - r.ce.prev_oi for r in ch.rows)
    pe = sum(r.pe.oi - r.pe.prev_oi for r in ch.rows)
    assert abs(s["call_oi_change"] - ce) < 1e-6 and abs(s["put_oi_change"] - pe) < 1e-6


def test_a_chain_without_prev_oi_still_summarises():
    ch = make_chain()
    for r in ch.rows:
        r.ce.prev_oi = None
        r.pe.prev_oi = None
    s = analytics.chain_summary(ch, 3)
    assert s["atm_iv"] is not None and s["call_oi_change"] == 0.0
