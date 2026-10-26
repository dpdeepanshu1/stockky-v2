"""E197: the invalid-IP early return in evaluate_mode must still leave a WAIT decision row for the picks it never reached
and close the Board shortlist (outcomes), instead of silently dropping consumed candidates."""
from __future__ import annotations

from tests.test_entry_evaluate_mode_remaining_coverage_2 import (  # noqa: F401
    _async_regime_ok, approve_all, db, make_candidate, no_notify, pin, quotes, run, tick,
)

import config
import models
from entry_engine import entry
from entry_engine import shortlist


def _wire(monkeypatch):
    monkeypatch.setattr(entry, "_get_market_regime", _async_regime_ok)
    monkeypatch.setattr(entry.dhan_client, "get_security_id", lambda db_, sym: "999")
    monkeypatch.setattr(entry.shared_order_budget, "check_and_reserve", lambda db_: True)
    monkeypatch.setattr(entry.shared_symbol_lock, "try_claim", lambda db_, sym, mode: True)
    monkeypatch.setattr(entry.shared_symbol_lock, "release", lambda db_, sym: None)

    def _boom(db_, **kw):
        raise RuntimeError("DH-907: invalid IP address")
    monkeypatch.setattr(entry.dhan_client, "place_order", _boom)
    monkeypatch.setattr("auth.dhan_credentials.disarm_on_invalid_ip", lambda db_, mode, err: True)
    no_notify(monkeypatch)


def _two(db, monkeypatch):
    make_candidate(db, symbol="AAA", mode="REAL", conviction=90.0)
    make_candidate(db, symbol="BBB", mode="REAL", conviction=80.0)
    quotes(monkeypatch, {"AAA": tick(100.0, symbol="AAA"), "BBB": tick(100.0, symbol="BBB")})
    approve_all(monkeypatch)
    monkeypatch.setattr(config, "ENTRY_MAX_NEW_PER_CYCLE", 1)
    monkeypatch.setattr(config, "ENTRY_CYCLE_QUALITY_FILTER_ENABLED", True)
    _wire(monkeypatch)


def test_reserve_pick_gets_a_wait_row_after_invalid_ip_return(db, monkeypatch):
    _two(db, monkeypatch)
    tally = run(entry.evaluate_mode(db, "REAL", gate_armed=True))
    assert tally.get("auto_disarmed") == "invalid_ip"
    rows = {d.symbol: d for d in db.query(models.TradeDecision).filter_by(mode="REAL").all()}
    assert set(rows) == {"AAA", "BBB"}
    assert all(r.action == "WAIT" for r in rows.values())
    assert any("auto-paused" in (r.reasoning or "") for r in rows.values())
    assert tally["waited"] == 2


def test_shortlist_outcomes_are_closed_after_invalid_ip_return(db, monkeypatch):
    _two(db, monkeypatch)
    run(entry.evaluate_mode(db, "REAL", gate_armed=True))
    picks = shortlist.snapshot("REAL")["picks"]
    assert picks and all(p["outcome"] for p in picks)
