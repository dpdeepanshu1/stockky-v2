"""Shared test helper: the `db` fixture in test_entry_evaluate_mode_remaining_coverage_2 already seeds one DEMO and one REAL
TradeAccount (trade_accounts.mode is UNIQUE), so a test that needs particular balances must update that row, not add a second."""
import models


def seed_account(db, mode, **fields):
    row = db.query(models.TradeAccount).filter_by(mode=mode).first()
    if row is None:
        row = models.TradeAccount(mode=mode)
        db.add(row)
    for k, v in fields.items():
        setattr(row, k, v)
    return row
