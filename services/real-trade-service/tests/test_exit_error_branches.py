"""Phase 1 (100%-coverage plan, session77 part 2): the remaining unrecognized-
error branches inside exit_engine.exit._send_real_sell's except block --
CDSL/eDIS, insufficient-funds, oversell (all 3 sub-cases), exchange-not-allowed
(EXCH:16387), and the two _cutoff_key siblings (intraday-cutoff,
security-intraday-restricted) -- plus the generic-rejection streak escalation
path. All of these previously had zero direct coverage of the actual
_send_real_sell code paths (lines 684-745, 776-812, 833-880, 857-880, 896-1001,
1044-1096 per the coverage report), even though _bump_exit_failure's pure logic
was already unit-tested in test_exit_backoff_escalation.py.

Run from services/real-trade-service:
    python -m pytest tests/test_exit_error_branches.py -q
"""
import asyncio, os, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from execution import dhan_client
from resilience.local_cache import load_snapshot, save_snapshot
from tz_utils import ist_today_str
import exit_engine.exit as ex

_engine = create_engine("sqlite:///:memory:")


def _mkposition(db, **kw):
    defaults = dict(
        mode="REAL", symbol="TESTSTOCK", status="OPEN", qty_open=10,
        avg_entry_price=100.0, opened_at=datetime.now(timezone.utc),
        broker_imported=True, current_stop=95.0, current_target=115.0,
        unrealized_pnl=250.0,
    )
    defaults.update(kw)
    pos = models.TradePosition(**defaults)
    db.add(pos); db.commit()
    return pos


@pytest.fixture()
def env(monkeypatch):
    models.Base.metadata.drop_all(_engine); models.Base.metadata.create_all(_engine)
    db = sessionmaker(bind=_engine)()
    monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "1234")
    monkeypatch.setattr(dhan_client, "round_to_tick", lambda p: round(p, 2))
    return db


def _sell(db, pos, **kw):
    return asyncio.run(ex._send_real_sell(db, pos, kw.pop("qty", 10), kw.pop("reason", "stop_hit"), **kw)) \
        if asyncio.iscoroutinefunction(ex._send_real_sell) \
        else ex._send_real_sell(db, pos, kw.pop("qty", 10), kw.pop("reason", "stop_hit"), **kw)


def _boom(message):
    def _raise(*a, **k):
        raise RuntimeError(message)
    return _raise


# ── CDSL / eDIS ────────────────────────────────────────────────────────────

def test_cdsl_error_alerts_once_then_suppresses_within_cooldown(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("Dhan API error: Validate Qty from CDSL"))
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: alerts.append(a[0] if a else k.get("msg")))

    assert _sell(db, pos) is False
    assert len(alerts) == 1
    assert "CDSL" in alerts[0]
    snap = load_snapshot(db, f"cdsl_alert_last_{pos.id}")
    assert snap and snap.get("at")

    # second failure within cooldown -> no new alert
    assert _sell(db, pos) is False
    assert len(alerts) == 1, "CDSL alert re-fired while still within cooldown"


def test_cdsl_error_alerts_again_after_cooldown_expires(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("cdsl edis required"))
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: alerts.append(True))
    save_snapshot(db, f"cdsl_alert_last_{pos.id}", {
        "at": (datetime.now(timezone.utc) - timedelta(minutes=ex.CDSL_ALERT_COOLDOWN_MIN + 5)).isoformat()
    })
    assert _sell(db, pos) is False
    assert len(alerts) == 1, "CDSL alert did not re-fire after cooldown window elapsed"


def test_cdsl_error_does_not_feed_the_generic_streak(env, monkeypatch):
    """CDSL is a persistent (_bump_exit_failure) error, not a generic-streak one --
    it must not touch exit_reject_streak_<id> (that snapshot key is generic-only)."""
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("cdsl"))
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    assert _sell(db, pos) is False
    assert load_snapshot(db, f"exit_reject_streak_{pos.id}") is None
    # but it IS persistent, so it does bump the exponential-backoff counter
    db.refresh(pos)
    assert pos.consecutive_exit_failures == 1


# ── Insufficient funds ──────────────────────────────────────────────────────

def test_insufficient_funds_alerts_once_then_suppresses(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("RMS:1102:You have insufficient funds. Please add Rs.500 to trade."))
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: alerts.append(a[0] if a else None))

    assert _sell(db, pos) is False
    assert len(alerts) == 1 and "insufficient" in alerts[0].lower()
    assert _sell(db, pos) is False
    assert len(alerts) == 1, "insufficient-funds alert re-fired while within cooldown"
    snap = load_snapshot(db, f"funds_alert_last_{pos.id}")
    assert snap and snap.get("at")


def test_insufficient_funds_is_persistent_and_bumps_streak(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("insufficient funds"))
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    assert _sell(db, pos) is False
    db.refresh(pos)
    assert pos.consecutive_exit_failures == 1


# ── Oversell: 3 sub-cases ───────────────────────────────────────────────────

def test_oversell_ghost_closes_when_broker_holds_zero(env, monkeypatch):
    db = env
    pos = _mkposition(db, qty_open=10)
    monkeypatch.setattr(dhan_client, "place_order", _boom("RMS:1:You are trying to sell more than the quantity you currently hold."))
    monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])  # broker holds nothing
    closed = {}
    monkeypatch.setattr(ex, "force_close_real_position", lambda db_, position, note: closed.update(note=note, symbol=position.symbol))
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)

    assert _sell(db, pos) is False
    assert closed.get("note") == "oversell_ghost_close"
    assert closed.get("symbol") == pos.symbol


def test_oversell_caps_qty_open_when_broker_holds_partial(env, monkeypatch):
    db = env
    pos = _mkposition(db, qty_open=10)
    monkeypatch.setattr(dhan_client, "place_order", _boom("trying to sell more than the quantity you currently hold"))
    monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
        {"tradingSymbol": pos.symbol, "availableQty": 4}
    ])
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: alerts.append(a[0] if a else None))

    assert _sell(db, pos) is False
    db.refresh(pos)
    assert pos.qty_open == 4
    assert any("synced" in m.lower() for m in alerts)


def test_oversell_no_ops_when_broker_qty_still_covers_ours(env, monkeypatch):
    """broker_qty >= our qty_open -> a timing issue, no position mutation, no
    ghost-close, no cap -- just a warning log and a retry next cycle."""
    db = env
    pos = _mkposition(db, qty_open=10)
    monkeypatch.setattr(dhan_client, "place_order", _boom("sell more than the quantity"))
    monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [
        {"tradingSymbol": pos.symbol, "availableQty": 10}
    ])
    closed = {"called": False}
    monkeypatch.setattr(ex, "force_close_real_position", lambda *a, **k: closed.update(called=True))
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)

    assert _sell(db, pos) is False
    db.refresh(pos)
    assert pos.qty_open == 10, "qty_open must not change on the timing-issue no-op path"
    assert closed["called"] is False


def test_oversell_holdings_sync_failure_alerts_once_then_suppresses(env, monkeypatch):
    db = env
    pos = _mkposition(db, qty_open=10)
    monkeypatch.setattr(dhan_client, "place_order", _boom("sell more than the quantity you currently hold"))

    def _sync_boom(db_):
        raise RuntimeError("holdings API down")
    monkeypatch.setattr(dhan_client, "get_holdings", _sync_boom)
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: alerts.append(a[0] if a else None))

    assert _sell(db, pos) is False
    assert len(alerts) == 1 and "sync failed" in alerts[0].lower()
    assert _sell(db, pos) is False
    assert len(alerts) == 1, "sync-failure alert re-fired while within cooldown"


def test_oversell_never_bumps_the_generic_streak(env, monkeypatch):
    db = env
    pos = _mkposition(db, qty_open=10)
    monkeypatch.setattr(dhan_client, "place_order", _boom("trying to sell more"))
    monkeypatch.setattr(dhan_client, "get_holdings", lambda db_: [])
    monkeypatch.setattr(ex, "force_close_real_position", lambda *a, **k: None)
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    assert _sell(db, pos) is False
    db.refresh(pos)
    assert pos.consecutive_exit_failures == 0
    assert load_snapshot(db, f"exit_reject_streak_{pos.id}") is None


# ── Exchange-not-allowed (EXCH:16387) ───────────────────────────────────────

def test_exchange_not_allowed_alerts_once_then_suppresses(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("EXCH:16387:Security is not allowed to trade in this market."))
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: alerts.append(a[0] if a else None))

    assert _sell(db, pos) is False
    assert len(alerts) == 1 and "T+1" in alerts[0]
    assert _sell(db, pos) is False
    assert len(alerts) == 1, "exchange-not-allowed alert re-fired within cooldown"


def test_exchange_not_allowed_is_persistent_and_bumps_streak(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("exch:16387"))
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    assert _sell(db, pos) is False
    db.refresh(pos)
    assert pos.consecutive_exit_failures == 1


# ── _cutoff_key siblings: intraday-cutoff and security-intraday-restricted ──

def test_intraday_cutoff_sets_cutoff_key_and_suppresses_resend(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    calls = {"n": 0}

    def _boom_count(*a, **k):
        calls["n"] += 1
        raise RuntimeError("Intraday orders cannot be placed at this time (square off time).")
    monkeypatch.setattr(dhan_client, "place_order", _boom_count)
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)

    assert _sell(db, pos) is False
    assert calls["n"] == 1
    cutoff_key = f"intraday_cutoff_hit_{pos.id}_{ist_today_str()}"
    assert load_snapshot(db, cutoff_key) == {"hit": True}

    for _ in range(5):
        assert _sell(db, pos) is False
    assert calls["n"] == 1, "SELL was re-sent to Dhan after the intraday cutoff was already hit today"


def test_intraday_cutoff_never_bumps_the_generic_streak(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("market is closed for intraday"))
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    assert _sell(db, pos) is False
    db.refresh(pos)
    assert pos.consecutive_exit_failures == 0


def test_security_intraday_restricted_sets_cutoff_key_and_suppresses_resend(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    calls = {"n": 0}

    def _boom_count(*a, **k):
        calls["n"] += 1
        raise RuntimeError("RMS:1:Order rejected as this stock is not allowed to be traded in Intraday.")
    monkeypatch.setattr(dhan_client, "place_order", _boom_count)
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)

    assert _sell(db, pos) is False
    assert calls["n"] == 1
    cutoff_key = f"intraday_cutoff_hit_{pos.id}_{ist_today_str()}"
    assert load_snapshot(db, cutoff_key) == {"hit": True}

    for _ in range(5):
        assert _sell(db, pos) is False
    assert calls["n"] == 1, "SELL was re-sent to Dhan after a security-level intraday restriction was already hit today"


def test_security_intraday_restricted_records_restriction_and_never_bumps_streak(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("not allowed to be traded in intraday"))
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    recorded = {}
    import intraday_eligibility
    monkeypatch.setattr(intraday_eligibility, "record_restriction",
                         lambda db_, symbol, reason: recorded.update(symbol=symbol, reason=reason))

    assert _sell(db, pos) is False
    assert recorded == {"symbol": pos.symbol, "reason": "stop_hit"}
    db.refresh(pos)
    assert pos.consecutive_exit_failures == 0


def test_security_intraday_restricted_survives_record_restriction_failure(env, monkeypatch):
    """record_restriction is explicitly best-effort -- a DB hiccup there must not
    prevent the alert/skip handling that follows it."""
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("not allowed to be traded in intraday"))
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: alerts.append(True))
    import intraday_eligibility
    monkeypatch.setattr(intraday_eligibility, "record_restriction",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup")))

    assert _sell(db, pos) is False
    assert len(alerts) == 1
    cutoff_key = f"intraday_cutoff_hit_{pos.id}_{ist_today_str()}"
    assert load_snapshot(db, cutoff_key) == {"hit": True}


# ── Generic-rejection streak escalation (end-to-end through _send_real_sell) ─

def test_generic_rejection_escalates_at_threshold_then_suppresses(env, monkeypatch):
    """Alerting is cooldown-throttled, not one-per-rejection: the very first
    rejection alerts plainly, then (within the same cooldown window) nothing
    fires again until the streak first crosses EXIT_REJECT_STREAK_ESCALATE_AT,
    which fires exactly one distinct 'STUCK' escalation -- after which it goes
    quiet again (escalated=True + still within cooldown) even though the
    streak count keeps climbing underneath."""
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("RMS:9:Some unrecognized rejection reason"))
    alerts = []
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: alerts.append(a[0] if a else None))
    threshold = ex.EXIT_REJECT_STREAK_ESCALATE_AT

    # 1st rejection: streak=1, no prior alert -> fires the plain "SELL rejected" alert
    assert _sell(db, pos) is False
    assert len(alerts) == 1
    assert "STUCK" not in alerts[0]

    # rejections 2..threshold-1: streak below the escalation threshold AND still
    # within the cooldown from the 1st alert -> stays completely silent
    for _ in range(threshold - 2):
        assert _sell(db, pos) is False
    assert len(alerts) == 1, "an intermediate rejection alerted while within cooldown and below threshold"

    # the rejection that pushes streak to exactly `threshold` -> escalation fires
    # regardless of cooldown (the escalation branch doesn't gate on `due`)
    assert _sell(db, pos) is False
    assert len(alerts) == 2
    assert "STUCK" in alerts[-1]
    assert f"{threshold} consecutive" in alerts[-1]

    snap = load_snapshot(db, f"exit_reject_streak_{pos.id}")
    assert snap["escalated"] is True
    assert snap["count"] == threshold

    # one more rejection, still within cooldown -> stays silent (streak keeps counting)
    assert _sell(db, pos) is False
    assert len(alerts) == 2, "escalation alert re-fired while still within cooldown"
    snap2 = load_snapshot(db, f"exit_reject_streak_{pos.id}")
    assert snap2["count"] == threshold + 1

    # advance the clock past cooldown -> the escalated state (streak still >=
    # threshold) re-alerts rather than staying silent forever
    snap2["last_alert_at"] = (datetime.now(timezone.utc) - timedelta(minutes=ex.CDSL_ALERT_COOLDOWN_MIN + 5)).isoformat()
    save_snapshot(db, f"exit_reject_streak_{pos.id}", snap2)
    assert _sell(db, pos) is False
    assert len(alerts) == 3, "escalation never re-fired after the cooldown window elapsed"
    assert "STUCK" in alerts[-1]


def test_generic_rejection_different_errors_share_one_streak_counter(env, monkeypatch):
    """A stream of DIFFERENT unrecognized errors must still increment one shared
    counter for the position -- the streak is per-position, not per-error-string."""
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)

    messages = [
        "RMS:1:First unrecognized reason",
        "RMS:2:A totally different unrecognized reason",
        "RMS:3:Yet another one nobody has seen before",
    ]
    for msg in messages:
        monkeypatch.setattr(dhan_client, "place_order", _boom(msg))
        assert _sell(db, pos) is False

    snap = load_snapshot(db, f"exit_reject_streak_{pos.id}")
    assert snap["count"] == len(messages)


def test_generic_rejection_bumps_exponential_backoff_counter(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    monkeypatch.setattr(dhan_client, "place_order", _boom("some unrecognized rejection"))
    monkeypatch.setattr(ex, "notify_sync", lambda *a, **k: None)
    threshold = ex.EXIT_REJECT_STREAK_ESCALATE_AT

    # below threshold: is_persistent=False and streak < escalate_at -> _bump_exit_failure
    # does NOT bump consecutive_exit_failures (mirrors test_exit_backoff_escalation's
    # unit-level assertion, now proven through the real _send_real_sell call site)
    for _ in range(threshold - 1):
        assert _sell(db, pos) is False
    db.refresh(pos)
    assert pos.consecutive_exit_failures == 0

    # at/above threshold: current_streak >= escalate_at -> _bump_exit_failure DOES bump
    assert _sell(db, pos) is False
    db.refresh(pos)
    assert pos.consecutive_exit_failures == 1
