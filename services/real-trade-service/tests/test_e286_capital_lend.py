"""E286 round 217: Real Trade may use Position Stocks' idle half (execution/capital_lend.py + the two call sites). Plain runner: python3 tests/test_e286_capital_lend.py."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from execution import capital_lend as cl


def test_jswenergy_case_spendable_cash_rises_above_the_fixed_half():
    # Dhan free 9,038; Real Trade holds nothing; Position Stocks holds 2,599. Fixed share = 4,519.
    c = cl.spendable_cash(9038.0, 0.0, 2599.0, 50.0, 0.5)
    assert c is not None and c > 4519.0 and c <= 9038.0
    assert abs(c - (9038.0 - 0.5 * (11637.0 * 0.5 - 2599.0))) < 0.01          # free cash minus the reserve kept for the peer


def test_reserve_frac_one_keeps_the_peers_whole_unused_allowance_free():
    idle = 11637.0 * 0.5 - 2599.0
    assert abs(cl.spendable_cash(9038.0, 0.0, 2599.0, 50.0, 1.0) - (9038.0 - idle)) < 0.01
    assert cl.spendable_cash(9038.0, 0.0, 2599.0, 50.0, 1.0) < cl.spendable_cash(9038.0, 0.0, 2599.0, 50.0, 0.5)


def test_reserve_frac_zero_lends_all_idle_and_is_capped_at_free_cash():
    assert cl.spendable_cash(9038.0, 0.0, 0.0, 50.0, 0.0) == 9038.0


def test_peer_using_its_whole_half_lends_nothing_on_the_exposure_cap():
    # peer exposure already above its 50 % of total: nothing idle to lend, the exposure cap stays the fixed share
    assert cl.exposure_cap(11000.0, 6000.0, 50.0, 0.5) == 5500.0
    assert cl.peer_idle(11000.0, 6000.0, 50.0) == 0.0


def test_exposure_cap_adds_the_lent_part_to_the_fixed_share():
    total = 9038.0 + 0.0 + 2599.0
    cap = cl.exposure_cap(total, 2599.0, 50.0, 0.5)
    assert cap > total * 0.5 and cap <= total - 2599.0


def test_stale_peer_or_bad_data_means_the_fixed_share():
    assert cl.spendable_cash(9038.0, 0.0, 2599.0, 50.0, 0.5, peer_fresh=False) is None
    assert cl.exposure_cap(11637.0, 2599.0, 50.0, 0.5, peer_fresh=False) is None
    assert cl.spendable_cash("x", 0, 0, 50, 0.5) is None
    assert cl.exposure_cap(0, 0, 50, 0.5) is None
    assert cl.peer_idle(None, 1, 50) == 0.0


def test_never_below_fixed_share_and_never_above_free_cash():
    for peer in (0.0, 1000.0, 3000.0, 8000.0):
        for fr in (0.0, 0.25, 0.5, 1.0):
            c = cl.spendable_cash(10000.0, 2000.0, peer, 50.0, fr)
            assert 5000.0 - 1e-6 <= c <= 10000.0 + 1e-6


def test_config_defaults():
    import config
    assert config.CAPITAL_LEND_MODE == "live" and config.CAPITAL_LEND_RESERVE_FRAC == 0.5 and config.CAPITAL_LEND_PEER_MAX_AGE_S == 900.0


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_") and callable(f):
            f()
            n += 1
    print(f"{n} passed")
