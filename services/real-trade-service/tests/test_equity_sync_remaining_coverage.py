"""
tests/test_equity_sync_remaining_coverage.py — closes execution/
equity_sync.py's last 2 coverage gaps (coverage plan, real-trade-service
round: was 94%, missing lines 79-80 and 114).

test_capital_share.py already covers sync_real_equity()'s main flow
(capital-share capping, Dhan-down, not-connected, unrecognized funds
shape, starting_capital auto-correction). This file adds the two branches
that flow didn't reach:

  - lines 79-80: _pick_balance()'s per-key except (TypeError, ValueError)
    -> continue to the next key, when a matched key's value can't be
    coerced to float (e.g. Dhan returns a non-numeric string for it).
  - line 114: sync_real_equity()'s "matched balance key changed, and it
    had already been set at least once before" warning branch — the
    _last_balance_key is None -> "using field X" branch is already
    covered by test_capital_share.py's fresh-fixture tests; this is the
    X -> Y "field changed" branch, which needs two syncs in a row with
    different matched keys.

Same patch strategy and fixture conventions as test_capital_share.py.

Run from services/real-trade-service:
    python -m pytest tests/test_equity_sync_remaining_coverage.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from execution import dhan_client, equity_sync

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def fresh_db(monkeypatch):
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    Session = sessionmaker(bind=_engine)
    db = Session()

    acct = models.TradeAccount(
        mode="REAL",
        cash_available=0.0,
        current_equity=0.0,
        broker_cash_available=0.0,
        starting_capital=0.0,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(acct)
    db.commit()

    monkeypatch.setattr(equity_sync, "_last_balance_key", None)
    monkeypatch.setattr(equity_sync, "_open_positions_market_value", lambda db_, mode: 0.0)

    yield db
    db.close()


# ── _pick_balance: non-numeric value skipped, next key tried ──────────────

def test_pick_balance_skips_non_numeric_value_and_tries_next_key():
    """A matched key whose value can't become a float must not blow up
    the whole sync — it's skipped, same as a missing key, and the next
    key in _BALANCE_KEYS order is tried."""
    funds = {
        "availabelBalance": "not-a-number",  # first key, present but garbage
        "availableBalance": 55_000.0,         # second key, good value
    }
    balance, matched_key = equity_sync._pick_balance(funds)
    assert balance == 55_000.0
    assert matched_key == "availableBalance"


def test_pick_balance_skips_none_valued_first_key_too():
    """Sanity check the two failure modes (None vs. non-numeric) both fall
    through the same way."""
    funds = {"availabelBalance": None, "withdrawableBalance": 12_000.0}
    balance, matched_key = equity_sync._pick_balance(funds)
    assert balance == 12_000.0
    assert matched_key == "withdrawableBalance"


def test_pick_balance_all_keys_non_numeric_returns_none():
    funds = {k: "garbage" for k in equity_sync._BALANCE_KEYS}
    balance, matched_key = equity_sync._pick_balance(funds)
    assert balance is None and matched_key is None


def test_sync_real_equity_recovers_when_first_matching_key_is_garbage(monkeypatch, fresh_db):
    """End-to-end: a real sync still succeeds off the second key when the
    first one Dhan returned can't be parsed as a number."""
    monkeypatch.setattr(
        dhan_client, "get_funds",
        lambda db_: {"availabelBalance": "N/A", "availableCash": 30_000.0},
    )
    equity = equity_sync.sync_real_equity(fresh_db)
    assert equity is not None


# ── matched-key-changed warning: second-time (not first-time) branch ──────

def test_balance_key_change_after_first_sync_is_tracked(monkeypatch, fresh_db, caplog):
    """First sync sets _last_balance_key (the 'is None' branch, already
    covered elsewhere). A second sync with a DIFFERENT matched key must
    take the other branch (line 114) and update _last_balance_key again —
    this is the actual production signal the module's docstring describes
    ('Dhan stops populating X and this silently starts falling through to
    Y instead')."""
    monkeypatch.setattr(dhan_client, "get_funds", lambda db_: {"availabelBalance": 40_000.0})
    equity_sync.sync_real_equity(fresh_db)
    assert equity_sync._last_balance_key == "availabelBalance"

    monkeypatch.setattr(dhan_client, "get_funds", lambda db_: {"sodLimit": 41_000.0})
    with caplog.at_level("WARNING"):
        equity_sync.sync_real_equity(fresh_db)

    assert equity_sync._last_balance_key == "sodLimit"
    assert any("changed" in r.getMessage() for r in caplog.records)


def test_balance_key_unchanged_across_syncs_does_not_re_warn(monkeypatch, fresh_db, caplog):
    """Same matched key on consecutive syncs must NOT re-trigger the
    changed-key warning (the `if matched_key != _last_balance_key` guard)."""
    monkeypatch.setattr(dhan_client, "get_funds", lambda db_: {"availabelBalance": 40_000.0})
    equity_sync.sync_real_equity(fresh_db)

    caplog.clear()
    with caplog.at_level("WARNING"):
        equity_sync.sync_real_equity(fresh_db)

    assert not any("changed" in r.getMessage() for r in caplog.records)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
