"""E215: /trades/history rows carry per-trade charges (same formula as the charges ledger)."""
from types import SimpleNamespace as NS

import main
from orders import charges_ledger


def _row(**kw):
    base = dict(status="STOP_HIT", side="BUY", entry_price=100.0, exit_price=99.0, quantity=5, realized_pnl=-5.0,
                exit_reason="stop_hit", closed_at=None, opened_at=None)
    base.update(kw)
    return NS(**base)


def test_settled_row_matches_ledger_formula():
    r = _row()
    want = round(charges_ledger.charges_for_trade(100.0, 99.0, 5)["total"], 2)
    assert main._row_charges(r) == want and want > 0


def test_short_uses_the_short_legs():
    r = _row(side="SHORT", entry_price=100.0, exit_price=101.0, realized_pnl=-5.0)
    want = round(charges_ledger.charges_for_trade(100.0, 101.0, 5, short=True)["total"], 2)
    assert main._row_charges(r) == want


def test_unsettled_or_unpriced_row_is_none():
    assert main._row_charges(_row(exit_price=None)) is None
    assert main._row_charges(_row(quantity=0)) is None
    # A real unresolved exit keeps its status and carries the *_PENDING_RECONCILE marker in error_message (exit_price is
    # still the entry placeholder), which trade_stats.classify treats as "pending", not settled.
    assert main._row_charges(_row(error_message="STOP_HIT_PENDING_RECONCILE: exit_price=entry_price placeholder")) is None
