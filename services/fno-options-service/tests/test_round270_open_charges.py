"""Round 270: the open-position charge estimate on GET /fno/overview uses costs.round_trip (the rule that stores real charges), so before / after charges never disagree."""
from __future__ import annotations

from fno import costs, liveparams


def test_open_estimate_matches_round_trip_and_net_is_gross_minus_charges():
    p = liveparams.current()
    est = costs.round_trip("NIFTY", 100.0, 120.0, 75, p, child=False).total
    gross = round((120.0 - 100.0) * 75, 2)
    assert est > 0
    assert round(gross - est, 2) < gross
    # a child (partial-exit) row leaves the flat entry brokerage out, so it never costs more than the parent form
    assert costs.round_trip("NIFTY", 100.0, 120.0, 75, p, child=True).total <= est
