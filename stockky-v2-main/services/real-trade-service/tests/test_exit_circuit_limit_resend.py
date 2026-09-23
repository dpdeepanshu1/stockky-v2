"""Session74: a SELL rejected as a lower/upper-circuit-band violation must not be
resent to Dhan on the next exit cycle -- it's permanent for the rest of the trading
session (same guarantee _cutoff_key already gives the intraday-cutoff and
security-intraday-restricted branches). Previously this branch never set that flag,
so a circuit-locked position was retried every EXIT_CHECK_INTERVAL_SECONDS all day.
Run from services/real-trade-service:  python -m pytest tests/test_exit_circuit_limit_resend.py -q"""
import asyncio, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from execution import dhan_client
import exit_engine.exit as ex

_engine = create_engine("sqlite:///:memory:")


@pytest.fixture()
def env(monkeypatch):
    models.Base.metadata.drop_all(_engine); models.Base.metadata.create_all(_engine)
    db = sessionmaker(bind=_engine)()
    pos = models.TradePosition(mode="REAL", symbol="LOWCKT", status="OPEN", qty_open=10, avg_entry_price=100.0,
                               opened_at=datetime.now(timezone.utc), broker_imported=True)
    db.add(pos); db.commit()
    calls = {"n": 0}
    monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "1234")

    def _boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("RMS:1102:Rate Not Within Ckt Limit 395.25 To 592.85")
    monkeypatch.setattr(dhan_client, "place_order", _boom)
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    return db, pos, calls


def _sell(db, pos):
    return asyncio.run(ex._send_real_sell(db, pos, 10, "stop_hit")) if asyncio.iscoroutinefunction(ex._send_real_sell) \
        else ex._send_real_sell(db, pos, 10, "stop_hit")


def test_circuit_limit_stops_resending_for_the_rest_of_today(env):
    db, pos, calls = env
    assert _sell(db, pos) is False and calls["n"] == 1
    # A circuit hit is not a broker/account persistent-error, so it must NOT
    # feed the generic exponential-backoff counter -- confirms the fix didn't
    # accidentally start treating it as `is_persistent`.
    assert (pos.consecutive_exit_failures or 0) == 0
    for _ in range(5):
        assert _sell(db, pos) is False
    assert calls["n"] == 1, "SELL was re-sent to Dhan while still circuit-locked (retry storm)"


def test_circuit_limit_does_not_get_flagged_intraday_restricted(env, monkeypatch):
    """Sanity check: the fix must reuse the existing _cutoff_key gate, not the
    persistent restricted-symbols list (that's for T2T/ASM, not a temporary
    circuit hit -- see the branch's own comment)."""
    db, pos, calls = env
    recorded = {"called": False}
    import intraday_eligibility
    monkeypatch.setattr(intraday_eligibility, "record_restriction", lambda *a, **k: recorded.__setitem__("called", True))
    _sell(db, pos)
    assert recorded["called"] is False
