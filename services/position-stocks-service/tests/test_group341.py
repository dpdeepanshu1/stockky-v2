"""group341 (log review 2026-10-13): pool baseline margin fraction, per-slot notional cap, quality-gate result cache + event memory,
leg re-arm retry with stop read-back, tick-buffer cap."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import config


# ---------------------------------------------------------------- ledger (items 1, 2)
@pytest.fixture()
def db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models
    from capital import ledger, shared_exposure
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    monkeypatch.setattr(ledger, "_last_balance_key", None)
    monkeypatch.setattr(ledger, "_sync_log_state", {})
    monkeypatch.setattr(ledger, "sync_peer_pnl", lambda d: None)
    monkeypatch.setattr(shared_exposure, "publish_own_exposure", lambda d, v: None)
    monkeypatch.setattr(shared_exposure, "get_other_service_exposure", lambda d: 0.0)
    monkeypatch.setattr(config, "SCALP_POOL_CAPITAL_SHARE_PCT", 50.0)
    yield s
    s.close()


def _open_position(db, capital_risked):
    import models
    db.add(models.ScalpPosition(
        symbol="TECHM", window_source="5m", adaptive_target_pct=1.44, adaptive_stop_pct=0.8, status="OPEN",
        quantity=2, entry_price=1489.3, capital_risked=capital_risked, overnight_converted_to_cnc=False,
        dhan_security_id="9", target_price=1510.7, stop_price=1477.4, opened_at=datetime.now(timezone.utc)))
    db.commit()


def test_committed_capital_added_back_at_margin_fraction(db, monkeypatch):
    from capital import ledger
    from execution import dhan_client
    monkeypatch.setattr(dhan_client, "get_funds", lambda d: {"availabelBalance": 10_000.0})
    _open_position(db, 3_000.0)
    monkeypatch.setattr(config, "SCALP_POOL_COMMITTED_MARGIN_FRAC", 0.20)
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(5_000.0 + 600.0)


def test_margin_fraction_one_is_the_old_behaviour(db, monkeypatch):
    from capital import ledger
    from execution import dhan_client
    monkeypatch.setattr(dhan_client, "get_funds", lambda d: {"availabelBalance": 10_000.0})
    _open_position(db, 3_000.0)
    monkeypatch.setattr(config, "SCALP_POOL_COMMITTED_MARGIN_FRAC", 1.0)
    ledger.sync_from_broker(db)
    assert ledger._get_or_create(db).total_allocated_capital == pytest.approx(8_000.0)


def _pool(db, total):
    from capital import ledger
    row = ledger._get_or_create(db)
    row.total_allocated_capital = total
    row.available_capital = total
    db.commit()
    return row


def test_tight_stop_is_capped_at_a_slot_multiple(db, monkeypatch):
    from capital import ledger
    monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 5)
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 2.0)
    monkeypatch.setattr(config, "SCALP_SLOT_NOTIONAL_CAP_MULT", 1.5)
    row = _pool(db, 10_000.0)
    value = ledger.reserve_capital(db, adaptive_stop_pct=0.8)      # risk/stop alone = 5,000 (half the pool)
    assert value == pytest.approx(3_000.0)                          # 10,000 / 5 x 1.5
    assert row.available_capital == pytest.approx(7_000.0)


def test_slot_cap_off_keeps_risk_over_stop(db, monkeypatch):
    from capital import ledger
    monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 5)
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 2.0)
    monkeypatch.setattr(config, "SCALP_SLOT_NOTIONAL_CAP_MULT", 0.0)
    _pool(db, 10_000.0)
    assert ledger.reserve_capital(db, adaptive_stop_pct=0.8) == pytest.approx(5_000.0)


def test_normal_stop_is_below_the_cap(db, monkeypatch):
    from capital import ledger
    monkeypatch.setattr(config, "MAX_CONCURRENT_SCALP_POSITIONS", 5)
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 2.0)
    monkeypatch.setattr(config, "SCALP_SLOT_NOTIONAL_CAP_MULT", 1.5)
    _pool(db, 10_000.0)
    assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) == pytest.approx(2_000.0)


# ---------------------------------------------------------------- quality gate (item 5)
class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture()
def qg(monkeypatch):
    from screening import quality_gate as q
    q.reset_runtime_state()
    monkeypatch.setattr(config, "QUALITY_GATE_ENABLED", True)
    monkeypatch.setattr(config, "QUALITY_GATE_RESULT_TTL_S", 90.0)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_MEMORY_H", 24.0)
    monkeypatch.setattr(q.httpx, "AsyncClient", lambda *a, **k: _FakeClient())
    yield q
    q.reset_runtime_state()


def _fakes(q, monkeypatch, *, fund=(60.0, 55.0, 5000.0), event_ok=True, earnings=None, calls=None):
    async def _ft(client, symbol):
        if calls is not None:
            calls["ft"] += 1
        return fund

    async def _ev(client, symbol):
        if calls is not None:
            calls["ev"] += 1
        if event_ok:
            if earnings:
                q._earnings_seen[symbol] = earnings
            return True, 70.0, False
        q._event_failed.add(symbol)
        return None, None, False

    monkeypatch.setattr(q, "_fetch_fund_tech", _ft)
    monkeypatch.setattr(q, "_fetch_event_signal", _ev)


def test_full_result_is_reused_within_ttl(qg, monkeypatch):
    calls = {"ft": 0, "ev": 0}
    _fakes(qg, monkeypatch, calls=calls)
    a = asyncio.run(qg.check("IRCON"))
    b = asyncio.run(qg.check("IRCON"))
    assert calls == {"ft": 1, "ev": 1}
    assert a.technical_score == b.technical_score == 55.0 and a is not b


def test_ttl_zero_fetches_every_time(qg, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_RESULT_TTL_S", 0.0)
    calls = {"ft": 0, "ev": 0}
    _fakes(qg, monkeypatch, calls=calls)
    asyncio.run(qg.check("IRCON"))
    asyncio.run(qg.check("IRCON"))
    assert calls == {"ft": 2, "ev": 2}


def test_incomplete_result_is_not_cached(qg, monkeypatch):
    calls = {"ft": 0, "ev": 0}
    _fakes(qg, monkeypatch, fund=(60.0, None, 5000.0), calls=calls)
    asyncio.run(qg.check("IRCON"))
    asyncio.run(qg.check("IRCON"))
    assert calls["ft"] == 2


def test_failed_event_call_keeps_remembered_earnings(qg, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_RESULT_TTL_S", 0.0)
    _fakes(qg, monkeypatch, earnings="2026-10-13")
    first = asyncio.run(qg.check("TECHM"))
    assert first.earnings_date == "2026-10-13"
    _fakes(qg, monkeypatch, event_ok=False)
    second = asyncio.run(qg.check("TECHM"))
    assert second.earnings_date == "2026-10-13"


def test_event_memory_off_loses_the_date(qg, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_RESULT_TTL_S", 0.0)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_MEMORY_H", 0.0)
    _fakes(qg, monkeypatch, earnings="2026-10-13")
    asyncio.run(qg.check("TECHM"))
    _fakes(qg, monkeypatch, event_ok=False)
    assert asyncio.run(qg.check("TECHM")).earnings_date is None


# ---------------------------------------------------------------- leg re-arm (item 4)
class _Db:
    def commit(self):
        pass

    def rollback(self):
        pass


def _pos(**kw):
    base = dict(id=7, symbol="TECHM", quantity=2, entry_price=1489.3, adaptive_target_pct=1.44, adaptive_stop_pct=0.8,
                stop_moved_to_breakeven=False, stop_price=1480.0, target_price=1510.0, dhan_super_order_id="S1", status="OPEN")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture()
def rc(monkeypatch):
    from orders import reconcile as r
    from execution import dhan_client
    r._rearm_state.clear()
    monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
    monkeypatch.setattr(dhan_client, "round_to_tick", lambda p: round(p, 2))
    monkeypatch.setattr(r.notifier, "notify_fire_and_forget", lambda *a, **k: None)
    yield r
    r._rearm_state.clear()


def test_both_legs_in_place(rc, monkeypatch):
    from execution import dhan_client
    sent = []
    monkeypatch.setattr(dhan_client, "modify_super_order", lambda db, **k: sent.append(k["order_leg"]))
    monkeypatch.setattr(dhan_client, "verify_stop_leg", lambda db, **k: True)
    p = _pos()
    assert rc._rearm_legs(_Db(), p) is True
    assert sent == ["TARGET_LEG", "STOP_LOSS_LEG"] and p.id not in rc._rearm_state
    assert p.target_price == pytest.approx(1510.74, abs=0.01) and p.stop_price == pytest.approx(1477.39, abs=0.01)


def test_failed_stop_keeps_old_stop_and_is_retried_without_redoing_target(rc, monkeypatch):
    from execution import dhan_client
    sent = []
    fail = {"stop": True}

    def _mod(db, **k):
        sent.append(k["order_leg"])
        if k["order_leg"] == "STOP_LOSS_LEG" and fail["stop"]:
            raise RuntimeError("DH-905")

    monkeypatch.setattr(dhan_client, "modify_super_order", _mod)
    monkeypatch.setattr(dhan_client, "verify_stop_leg", lambda db, **k: True)
    p = _pos()
    assert rc._rearm_legs(_Db(), p) is False
    assert p.stop_price == 1480.0 and p.target_price != 1510.0       # target stored, stop untouched
    fail["stop"] = False
    rc._retry_rearm_pending(_Db(), p)
    assert sent == ["TARGET_LEG", "STOP_LOSS_LEG", "STOP_LOSS_LEG"]   # target not sent again
    assert p.stop_price == pytest.approx(1477.39, abs=0.01) and p.id not in rc._rearm_state


def test_stop_that_reads_back_wrong_is_not_stored(rc, monkeypatch):
    from execution import dhan_client
    monkeypatch.setattr(dhan_client, "modify_super_order", lambda db, **k: None)
    monkeypatch.setattr(dhan_client, "verify_stop_leg", lambda db, **k: False)
    p = _pos()
    assert rc._rearm_legs(_Db(), p) is False
    assert p.stop_price == 1480.0


def test_gives_up_after_max_tries(rc, monkeypatch):
    from execution import dhan_client

    def _boom(db, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(dhan_client, "modify_super_order", _boom)
    p = _pos()
    for _ in range(rc._REARM_MAX_TRIES):
        rc._retry_rearm_pending(_Db(), p) if p.id in rc._rearm_state else rc._rearm_legs(_Db(), p)
    assert p.id not in rc._rearm_state


def test_ratcheted_stop_is_not_pulled_down(rc, monkeypatch):
    from execution import dhan_client
    seen = {}
    monkeypatch.setattr(dhan_client, "modify_super_order", lambda db, **k: seen.update(k))
    monkeypatch.setattr(dhan_client, "verify_stop_leg", lambda db, **k: None)
    p = _pos(stop_moved_to_breakeven=True, stop_price=1495.0)
    rc._rearm_legs(_Db(), p)
    assert p.stop_price == 1495.0


# ---------------------------------------------------------------- tick buffer (item 6)
def test_tick_buffer_cap_follows_config_and_unknown_symbol_is_not_created():
    from feed import ws_client
    assert ws_client._MAX_TICKS == max(500, int(config.WS_MAX_TICKS_PER_SYMBOL or 4000))
    assert ws_client.get_tick_buffer("NOSUCHSYM341") == []
    assert "NOSUCHSYM341" not in ws_client._tick_buffers
