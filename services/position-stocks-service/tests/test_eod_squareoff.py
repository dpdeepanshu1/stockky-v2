"""
tests/test_eod_squareoff.py — offline tests for orders/eod_squareoff.py and
orders/exit_retry.py (position-stocks-service).

What is exercised, and why it matters: this is the code that decides what gets
sold at the end of the day, which positions are carried overnight, and what
happens when the broker says no. A bug here leaves real-money positions open
past the close (or sells something that should have been carried).

Everything runs offline:
  - real SQLAlchemy models on an in-memory SQLite DB (so ledger, symbol-lock and
    order-budget side effects are asserted for real, not mocked);
  - a scripted fake broker patched onto `execution.dhan_client`;
  - notifier, time.sleep, the IST date and the WebSocket tick buffer patched.

Sections:
  TestPlaceOvernightStop      protective-stop placement helper
  TestGateState               once-per-day gate row + lazy kill-switch reset
  TestFireFlatSell            one flat SELL: product type, retries, cooldown, stop cancel
  TestRunEodSquareoffPlain    the normal sweep (overnight carry OFF)
  TestRunEodFailureHandling   the 5 rejection classes + isolation between positions
  TestOvernightCarry          carry filter, pool cap, CNC conversion, stop placement
  TestCloseNow                manual / stagnation close_position_now()
  TestStagnationExit          run_stagnation_exit()
  TestExitRetry               exit-placement backoff module

Run from services/position-stocks-service:
    python3 -m pytest tests/test_eod_squareoff.py -q \\
        --cov=orders --cov-report=term-missing
"""
from __future__ import annotations

import itertools
import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import notifier
from capital import ledger, shared_symbol_lock
from execution import dhan_client
from feed import ws_client
from orders import eod_squareoff as eod
from orders import exit_retry

TODAY = "2026-09-21"
LEDGER_TOTAL = 100_000.0
LEDGER_AVAILABLE = 50_000.0

# Real Dhan-style rejection strings, one per classifier in dhan_client.
CUTOFF_MSG = "Intraday orders cannot be placed at this time"
RESTRICTED_MSG = "Scrip is not allowed to be traded in Intraday"
FUNDS_MSG = "RMS: Insufficient funds. Add Rs. 5000"
CIRCUIT_MSG = "RMS: Rate Not Within Ckt Limit 395.25 To 592.85"
GENERIC_MSG = "gateway timeout talking to broker"


# ── Fake broker ──────────────────────────────────────────────────────────────
class Broker:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []   # ordered log of everything the code did
        self.orders: list[dict] = []               # rows for get_order_list
        self.trades: list[dict] = []               # rows for get_trade_history
        self.ticks: dict[str, float] = {}          # symbol -> last price
        self.tick_raises: set[str] = set()
        self.place_script: list = []               # consumed one per place_order call
        self.place_by_sec: dict = {}               # security_id -> persistent outcome
        self.stop_script: list = []                # consumed one per stop placement
        self.convert_fail: set[str] = set()        # security_ids whose conversion fails
        self.cancel_super_fail = False
        self.cancel_stop_fail = False
        self.on_cancel_stop = None                 # hook run inside cancel_cnc_stop_loss_order
        self.edis = {"verified_today": True, "pending_symbols": []}
        self._n = 0
        self._sn = 0

    def names(self, *wanted: str) -> list[str]:
        return [c[0] for c in self.calls if c[0] in wanted]

    def of(self, name: str) -> list[dict]:
        return [c[1] for c in self.calls if c[0] == name]

    def sleeps(self) -> list[float]:
        return [c[1]["s"] for c in self.calls if c[0] == "sleep"]


def _outcome(o, default):
    if isinstance(o, BaseException):
        raise o
    return o if isinstance(o, dict) else default


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    b = Broker()
    sent = {"info": [], "critical": [], "boom": False}

    def _info(m, *a, **k):
        sent["info"].append(m)
        return True

    def _critical(m, *a, **k):
        if sent["boom"]:
            raise RuntimeError("telegram down")
        sent["critical"].append(m)

    monkeypatch.setattr(notifier, "notify_sync", _info)
    monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda m, *a, **k: _info(m))
    monkeypatch.setattr(notifier, "notify_critical", _critical)

    # ── broker surface ──
    def _place_order(db_, **kw):
        b.calls.append(("place_order", kw))
        if kw["security_id"] in b.place_by_sec:
            out = b.place_by_sec[kw["security_id"]]
        elif b.place_script:
            out = b.place_script.pop(0)
        else:
            out = None
        b._n += 1
        return _outcome(out, {"orderId": f"F{b._n}"})

    def _cancel_super(db_, *, order_id, order_leg):
        b.calls.append(("cancel_super_order", {"order_id": order_id, "order_leg": order_leg}))
        if b.cancel_super_fail:
            raise RuntimeError("leg already filled")
        return {}

    def _convert(db_, **kw):
        b.calls.append(("convert_position", kw))
        if kw["security_id"] in b.convert_fail:
            raise RuntimeError("insufficient margin for CNC conversion")
        return {}

    def _place_stop(db_, **kw):
        b.calls.append(("place_cnc_stop_loss_market", kw))
        b._sn += 1
        out = b.stop_script.pop(0) if b.stop_script else None
        return _outcome(out, {"orderId": f"S{b._sn}"})

    def _cancel_stop(db_, *, order_id):
        b.calls.append(("cancel_cnc_stop_loss_order", {"order_id": order_id}))
        if b.on_cancel_stop:
            b.on_cancel_stop()
        if b.cancel_stop_fail:
            raise RuntimeError("stop already gone")
        return {}

    def _order_list(db_):
        b.calls.append(("get_order_list", {}))
        return list(b.orders)

    def _edis(db_):
        b.calls.append(("edis_verification_summary", {}))
        if isinstance(b.edis, BaseException):
            raise b.edis
        return b.edis

    monkeypatch.setattr(dhan_client, "place_order", _place_order)
    monkeypatch.setattr(dhan_client, "cancel_super_order", _cancel_super)
    monkeypatch.setattr(dhan_client, "convert_position", _convert)
    monkeypatch.setattr(dhan_client, "place_cnc_stop_loss_market", _place_stop)
    monkeypatch.setattr(dhan_client, "cancel_cnc_stop_loss_order", _cancel_stop)
    monkeypatch.setattr(dhan_client, "get_order_list", _order_list)
    monkeypatch.setattr(dhan_client, "get_trade_history", lambda db_, *a, **k: list(b.trades))
    monkeypatch.setattr(dhan_client, "edis_verification_summary", _edis)

    def _tick_buffer(sym):
        if sym in b.tick_raises:
            raise RuntimeError("ws down")
        return [(0, b.ticks[sym])] if sym in b.ticks else []

    monkeypatch.setattr(ws_client, "get_tick_buffer", _tick_buffer)

    # ── time, date ──
    monkeypatch.setattr(eod, "time", types.SimpleNamespace(sleep=lambda s: b.calls.append(("sleep", {"s": s}))))
    monkeypatch.setattr(eod, "ist_today_str", lambda: TODAY)

    # ── pin every knob these paths read, independent of the VM's env vars ──
    pins = dict(
        SCALP_PRODUCT_TYPE="INTRADAY", SCALP_EXCHANGE_SEGMENT="NSE_EQ", USE_SUPER_ORDER=False,
        EOD_SELL_RETRY_ATTEMPTS=3, EOD_SELL_RETRY_DELAY_SECONDS=2.0, MANUAL_EXIT_CANCEL_WAIT_S=0.5,
        EXIT_RETRY_BASE_COOLDOWN_SECONDS=60.0, EXIT_RETRY_MAX_COOLDOWN_SECONDS=900.0,
        EXIT_RETRY_ALERT_THRESHOLD=5,
        OVERNIGHT_HOLD_ENABLED=False, OVERNIGHT_STOP_LOSS_PCT=4.0,
        OVERNIGHT_MIN_FUNDAMENTAL_SCORE=60.0, OVERNIGHT_MIN_TECHNICAL_SCORE=60.0,
        OVERNIGHT_MIN_MARKET_CAP_CR=2000.0, OVERNIGHT_HOLD_MAX_EXPOSURE_PCT_OF_POOL=30.0,
        STAGNATION_EXIT_MINUTES=45.0, STAGNATION_EXIT_BAND_PCT=0.35,
    )
    for k, v in pins.items():
        monkeypatch.setattr(config, k, v)

    led = ledger._get_or_create(db)
    led.total_allocated_capital, led.available_capital = LEDGER_TOTAL, LEDGER_AVAILABLE
    db.commit()
    yield db, b, sent
    db.close()


# ── Builders ─────────────────────────────────────────────────────────────────
_ids = itertools.count(1)


def mkpos(db, symbol=None, *, status="OPEN", entry=100.0, qty=10, capital=None, super_id=None,
          cnc=False, stop_id=None, opened_min_ago=60, claim_lock=True, **extra):
    n = next(_ids)
    symbol = symbol or f"SYM{n}"
    p = models.ScalpPosition(
        symbol=symbol, dhan_security_id=str(1000 + n), window_source="5m", status=status,
        entry_price=entry, quantity=qty, target_price=entry * 1.02, stop_price=entry * 0.99,
        adaptive_target_pct=2.0, adaptive_stop_pct=1.0,
        capital_risked=entry * qty if capital is None else capital,
        dhan_super_order_id=super_id, overnight_converted_to_cnc=cnc, overnight_stop_order_id=stop_id,
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=opened_min_ago), **extra,
    )
    db.add(p)
    db.commit()
    if claim_lock:
        shared_symbol_lock.try_claim(db, symbol)
    return p


def mklog(db, symbol, *, fund=70.0, tech=70.0, mcap=5000.0, decision="ENTERED", age_min=0):
    db.add(models.ScalpCandidateLog(
        symbol=symbol, window_source="5m", pct_change=1.0, decision=decision,
        fundamental_score=fund, technical_score=tech, market_cap_cr=mcap,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min),
    ))
    db.commit()


def qualifying(db, b, *, entry=100.0, ltp=105.0, qty=10, fund=70.0, tech=70.0, mcap=5000.0, **kw):
    """An OPEN in-profit position with a passing entry-time candidate-log row."""
    p = mkpos(db, entry=entry, qty=qty, **kw)
    mklog(db, p.symbol, fund=fund, tech=tech, mcap=mcap)
    b.ticks[p.symbol] = ltp
    return p


def order_row(oid, status, filled, avg, qty=10):
    return {"orderId": oid, "orderStatus": status, "filledQty": filled,
            "averageTradedPrice": avg, "quantity": qty}


def lock_held(db, symbol) -> bool:
    return db.query(models.SharedSymbolLock).filter_by(symbol=symbol.upper()).first() is not None


def available(db) -> float:
    return ledger.get_state(db)["available_capital"]


def fired_date(db):
    return db.query(models.ScalpGateState).filter_by(mode="REAL").first().eod_squareoff_fired_date


def enable_carry(monkeypatch):
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_ENABLED", True)


# ── _place_overnight_stop ────────────────────────────────────────────────────
class TestPlaceOvernightStop:
    def test_places_stop_at_pct_below_entry(self, env):
        db, b, _ = env
        p = mkpos(db, entry=200.0, qty=7)
        oid = eod._place_overnight_stop(db, p, 4.0)
        assert oid == "S1"
        kw = b.of("place_cnc_stop_loss_market")[0]
        assert kw["trigger_price"] == pytest.approx(192.0)
        assert kw["quantity"] == 7
        assert kw["security_id"] == p.dhan_security_id
        assert kw["exchange_segment"] == "NSE_EQ"
        assert kw["is_armed"] is True
        assert kw["tag"] == f"OVERNIGHT_STOP_{p.id}"

    def test_accepts_snake_case_order_id(self, env):
        db, b, _ = env
        b.stop_script = [{"order_id": "X9"}]
        assert eod._place_overnight_stop(db, mkpos(db), 4.0) == "X9"

    def test_missing_order_id_is_a_failure(self, env):
        db, b, sent = env
        b.stop_script = [{}]
        assert eod._place_overnight_stop(db, mkpos(db), 4.0) is None

    def test_broker_exception_returns_none_and_alerts(self, env):
        db, b, sent = env
        b.stop_script = [RuntimeError("RMS said no")]
        assert eod._place_overnight_stop(db, mkpos(db), 4.0) is None
        assert any("OVERNIGHT STOP PLACEMENT FAILED" in m for m in sent["critical"])

    def test_alert_failure_is_swallowed(self, env):
        db, b, sent = env
        sent["boom"] = True
        b.stop_script = [RuntimeError("RMS said no")]
        assert eod._place_overnight_stop(db, mkpos(db), 4.0) is None

    @pytest.mark.parametrize("pct", [100.0, 150.0])
    def test_non_positive_trigger_never_reaches_broker(self, env, pct):
        db, b, _ = env
        assert eod._place_overnight_stop(db, mkpos(db), pct) is None
        assert b.of("place_cnc_stop_loss_market") == []


# ── _get_gate_state ──────────────────────────────────────────────────────────
class TestGateState:
    def test_creates_real_row_once(self, env):
        db, _, _ = env
        g1 = eod._get_gate_state(db)
        g2 = eod._get_gate_state(db)
        assert g1.id == g2.id and g1.mode == "REAL"
        assert db.query(models.ScalpGateState).count() == 1

    def test_kill_switch_reset_on_new_day(self, env):
        db, _, _ = env
        g = eod._get_gate_state(db)
        g.daily_loss_kill_switch_tripped, g.daily_loss_kill_switch_tripped_date = True, "2026-09-18"
        db.commit()
        g = eod._get_gate_state(db)
        assert g.daily_loss_kill_switch_tripped is False
        assert g.daily_loss_kill_switch_tripped_date is None

    def test_kill_switch_kept_on_same_day(self, env):
        db, _, _ = env
        g = eod._get_gate_state(db)
        g.daily_loss_kill_switch_tripped, g.daily_loss_kill_switch_tripped_date = True, TODAY
        db.commit()
        assert eod._get_gate_state(db).daily_loss_kill_switch_tripped is True


# ── _fire_flat_sell ──────────────────────────────────────────────────────────
class TestFireFlatSell:
    def test_market_sell_arguments(self, env):
        db, b, _ = env
        p = mkpos(db, qty=25)
        res = eod._fire_flat_sell(db, p)
        assert res == {"orderId": "F1"}
        kw = b.of("place_order")[0]
        assert kw["is_armed"] is True                     # exits are never gated by the arm switch
        assert kw["transaction_type"] == "SELL"
        assert kw["order_type"] == "MARKET" and kw["price"] == 0.0
        assert kw["quantity"] == 25
        assert kw["security_id"] == p.dhan_security_id
        assert kw["exchange_segment"] == "NSE_EQ"
        assert kw["product_type"] == "INTRADAY"
        assert kw["tag"] == "EOD_SQUAREOFF"

    def test_intraday_product_follows_config(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "SCALP_PRODUCT_TYPE", "MIS")
        eod._fire_flat_sell(db, mkpos(db))
        assert b.of("place_order")[0]["product_type"] == "MIS"

    def test_cnc_holding_sells_as_cnc(self, env):
        db, b, _ = env
        eod._fire_flat_sell(db, mkpos(db, cnc=True))
        assert b.of("place_order")[0]["product_type"] == "CNC"

    def test_success_clears_failure_streak(self, env):
        db, b, _ = env
        p = mkpos(db, consecutive_exit_failures=2,
                  last_exit_failure_at=datetime.now(timezone.utc) - timedelta(hours=1))
        eod._fire_flat_sell(db, p)
        assert p.consecutive_exit_failures == 0 and p.last_exit_failure_at is None

    def test_transient_failures_are_retried(self, env):
        db, b, _ = env
        b.place_script = [RuntimeError(GENERIC_MSG), RuntimeError(GENERIC_MSG), {"orderId": "OK"}]
        p = mkpos(db)
        assert eod._fire_flat_sell(db, p) == {"orderId": "OK"}
        assert len(b.of("place_order")) == 3
        assert b.sleeps() == [2.0, 2.0]
        assert p.consecutive_exit_failures == 0

    def test_exhausted_retries_raise_last_error_and_record_failure(self, env):
        db, b, _ = env
        b.place_script = [RuntimeError("e1"), RuntimeError("e2"), RuntimeError("e3")]
        p = mkpos(db)
        with pytest.raises(RuntimeError, match="e3"):
            eod._fire_flat_sell(db, p)
        assert len(b.of("place_order")) == 3
        assert b.sleeps() == [2.0, 2.0]   # no sleep after the last try
        assert p.consecutive_exit_failures == 1
        assert p.last_exit_failure_at is not None

    @pytest.mark.parametrize("msg", [CUTOFF_MSG, RESTRICTED_MSG, FUNDS_MSG, CIRCUIT_MSG])
    def test_permanent_rejections_fail_fast_without_retry(self, env, msg):
        db, b, _ = env
        b.place_script = [RuntimeError(msg)]          # a 2nd attempt would get a default success
        p = mkpos(db)
        with pytest.raises(RuntimeError):
            eod._fire_flat_sell(db, p)
        assert len(b.of("place_order")) == 1
        assert b.sleeps() == []
        assert p.consecutive_exit_failures == 1       # still counted toward the cooldown

    @pytest.mark.parametrize("attempts", [0, -5])
    def test_attempts_floor_at_one(self, env, monkeypatch, attempts):
        db, b, _ = env
        monkeypatch.setattr(config, "EOD_SELL_RETRY_ATTEMPTS", attempts)
        b.place_script = [RuntimeError(GENERIC_MSG)]
        with pytest.raises(RuntimeError):
            eod._fire_flat_sell(db, mkpos(db))
        assert len(b.of("place_order")) == 1

    def test_active_cooldown_blocks_before_any_broker_call(self, env):
        db, b, _ = env
        # 3 failures -> 60 * 2^2 = 240s window; last failure 10s ago
        p = mkpos(db, cnc=True, stop_id="S1", consecutive_exit_failures=3,
                  last_exit_failure_at=datetime.now(timezone.utc) - timedelta(seconds=10))
        with pytest.raises(exit_retry.ExitInCooldown):
            eod._fire_flat_sell(db, p)
        assert b.calls == []                          # not even the stop cancel / order-list lookup

    def test_expired_cooldown_allows_the_sell(self, env):
        db, b, _ = env
        p = mkpos(db, consecutive_exit_failures=3,
                  last_exit_failure_at=datetime.now(timezone.utc) - timedelta(seconds=241))
        eod._fire_flat_sell(db, p)
        assert len(b.of("place_order")) == 1

    # ── resting overnight stop ──
    def test_resting_stop_is_cancelled_before_the_sell(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, stop_id="S1", qty=10)
        eod._fire_flat_sell(db, p)
        assert b.names("get_order_list", "cancel_cnc_stop_loss_order", "place_order") == [
            "get_order_list", "cancel_cnc_stop_loss_order", "get_order_list", "place_order"]
        assert b.of("cancel_cnc_stop_loss_order")[0] == {"order_id": "S1"}
        assert p.overnight_stop_order_id is None
        assert b.of("place_order")[0]["product_type"] == "CNC"

    def test_stop_filled_before_cancel_means_already_flat(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, stop_id="S1", qty=10)
        b.orders = [order_row("S1", "TRADED", 10, 96.0)]
        with pytest.raises(eod.PositionAlreadyFlat):
            eod._fire_flat_sell(db, p)
        assert b.of("place_order") == [] and b.of("cancel_cnc_stop_loss_order") == []
        assert p.status == "STOP_HIT"

    def test_partial_stop_fill_sizes_the_sell_to_the_remainder(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, stop_id="S1", qty=100)
        b.orders = [order_row("S1", "PART_TRADED", 30, 96.0, qty=100)]
        eod._fire_flat_sell(db, p)
        assert b.of("place_order")[0]["quantity"] == 70
        assert p.realized_pnl == pytest.approx(-120.0)      # (96-100) * 30 already booked

    def test_stop_fill_landing_during_cancel_means_already_flat(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, stop_id="S1", qty=10)
        b.on_cancel_stop = lambda: setattr(b, "orders", [order_row("S1", "TRADED", 10, 95.0)])
        with pytest.raises(eod.PositionAlreadyFlat):
            eod._fire_flat_sell(db, p)
        assert len(b.of("cancel_cnc_stop_loss_order")) == 1
        assert b.of("place_order") == []

    def test_cancel_failure_does_not_stop_the_sell(self, env):
        db, b, _ = env
        b.cancel_stop_fail = True
        p = mkpos(db, cnc=True, stop_id="S1")
        eod._fire_flat_sell(db, p)
        assert len(b.of("place_order")) == 1

    def test_no_stop_id_means_no_cancel_and_no_order_list_lookup(self, env):
        db, b, _ = env
        eod._fire_flat_sell(db, mkpos(db, cnc=True, stop_id=None))
        assert b.names("get_order_list", "cancel_cnc_stop_loss_order") == []


# ── run_eod_squareoff — plain sweep ──────────────────────────────────────────
class TestRunEodSquareoffPlain:
    def test_already_fired_today_is_a_noop(self, env):
        db, b, _ = env
        eod._get_gate_state(db).eod_squareoff_fired_date = TODAY
        db.commit()
        p = mkpos(db)
        assert eod.run_eod_squareoff(db) == 0
        assert b.calls == [] and p.status == "OPEN"

    def test_no_open_positions_still_sets_the_guard(self, env):
        db, b, _ = env
        mkpos(db, status="TARGET_HIT")
        assert eod.run_eod_squareoff(db) == 0
        assert fired_date(db) == TODAY
        assert b.of("place_order") == []

    def test_sells_every_open_position_and_books_placeholders(self, env):
        db, b, _ = env
        a = mkpos(db, "AAA", entry=100.0, qty=10)      # capital 1,000
        c = mkpos(db, "CCC", entry=50.0, qty=20)       # capital 1,000
        assert eod.run_eod_squareoff(db) == 2
        for p in (a, c):
            assert p.status == "EOD_SQUAREOFF"
            assert p.exit_price == p.entry_price       # placeholder until reconcile
            assert p.realized_pnl == 0.0 and p.realized_pnl_pct == 0.0
            assert p.closed_at is not None
            assert p.error_message.startswith("EOD_SQUAREOFF_PENDING_RECONCILE")
            assert p.dhan_exit_order_id in ("F1", "F2")
            assert not lock_held(db, p.symbol)
        assert {a.dhan_exit_order_id, c.dhan_exit_order_id} == {"F1", "F2"}
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 2_000.0)
        assert db.query(models.SharedOrderBudget).one().orders_placed_today == 2
        assert fired_date(db) == TODAY

    def test_only_open_and_exit_legs_rejected_are_swept(self, env):
        db, b, _ = env
        keep = [mkpos(db, status=s) for s in
                ("TARGET_HIT", "STOP_HIT", "ERROR", "EOD_SQUAREOFF", "MANUAL_EXIT", "PENDING")]
        opn = mkpos(db, status="OPEN")
        rej = mkpos(db, status="EXIT_LEGS_REJECTED")
        assert eod.run_eod_squareoff(db) == 2
        assert len(b.of("place_order")) == 2
        assert opn.status == rej.status == "EOD_SQUAREOFF"
        assert [p.status for p in keep] == ["TARGET_HIT", "STOP_HIT", "ERROR", "EOD_SQUAREOFF",
                                            "MANUAL_EXIT", "PENDING"]

    def test_second_call_same_day_places_nothing(self, env):
        db, b, _ = env
        mkpos(db)
        assert eod.run_eod_squareoff(db) == 1
        n = len(b.of("place_order"))
        mkpos(db)                                       # a fresh OPEN position appears afterwards
        assert eod.run_eod_squareoff(db) == 0
        assert len(b.of("place_order")) == n

    def test_sell_is_placed_even_when_gate_is_disarmed(self, env):
        db, b, _ = env
        g = eod._get_gate_state(db)
        g.is_armed = False
        db.commit()
        mkpos(db)
        assert eod.run_eod_squareoff(db) == 1
        assert b.of("place_order")[0]["is_armed"] is True

    def test_super_order_legs_cancelled_then_pause_then_sell(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
        p = mkpos(db, super_id="SO1")
        eod.run_eod_squareoff(db)
        seq = [(c[0], c[1].get("order_leg")) for c in b.calls
               if c[0] in ("cancel_super_order", "sleep", "place_order")]
        assert seq == [("cancel_super_order", "ENTRY_LEG"), ("cancel_super_order", "TARGET_LEG"),
                       ("cancel_super_order", "STOP_LOSS_LEG"), ("sleep", None), ("place_order", None)]
        assert all(c["order_id"] == "SO1" for c in b.of("cancel_super_order"))
        assert b.sleeps() == [0.5]

    def test_leg_cancel_failures_do_not_block_the_sell(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
        b.cancel_super_fail = True
        p = mkpos(db, super_id="SO1")
        assert eod.run_eod_squareoff(db) == 1
        assert p.status == "EOD_SQUAREOFF"

    def test_zero_cancel_wait_skips_the_pause(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
        monkeypatch.setattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0)
        mkpos(db, super_id="SO1")
        eod.run_eod_squareoff(db)
        assert b.sleeps() == []

    def test_super_order_disabled_never_cancels_legs(self, env):
        db, b, _ = env
        mkpos(db, super_id="SO1")                       # USE_SUPER_ORDER pinned False
        eod.run_eod_squareoff(db)
        assert b.of("cancel_super_order") == []

    def test_no_super_order_id_never_cancels_legs(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
        mkpos(db, super_id=None)
        eod.run_eod_squareoff(db)
        assert b.of("cancel_super_order") == []

    def test_booked_partial_pnl_is_preserved(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, overnight_stop_prior_qty=30, realized_pnl=-120.0, realized_pnl_pct=-1.2)
        eod.run_eod_squareoff(db)
        assert p.status == "EOD_SQUAREOFF"
        assert p.realized_pnl == pytest.approx(-120.0)
        assert p.realized_pnl_pct == pytest.approx(-1.2)

    def test_without_partials_pnl_is_zeroed_placeholder(self, env):
        db, b, _ = env
        p = mkpos(db, realized_pnl=5.0, realized_pnl_pct=0.5)
        eod.run_eod_squareoff(db)
        assert p.realized_pnl == 0.0 and p.realized_pnl_pct == 0.0

    def test_position_already_flat_counts_as_closed_but_is_not_overwritten(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, stop_id="S1", qty=10)
        b.orders = [order_row("S1", "TRADED", 10, 96.0)]
        assert eod.run_eod_squareoff(db) == 1
        assert p.status == "STOP_HIT"                    # left exactly as the stop settled it
        assert b.of("place_order") == []
        assert fired_date(db) == TODAY

    def test_cnc_position_with_live_stop_is_cancelled_then_sold_as_cnc(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, stop_id="S1")
        assert eod.run_eod_squareoff(db) == 1
        assert b.of("cancel_cnc_stop_loss_order")[0]["order_id"] == "S1"
        assert b.of("place_order")[0]["product_type"] == "CNC"
        assert p.status == "EOD_SQUAREOFF" and p.overnight_stop_order_id is None


# ── run_eod_squareoff — failure handling ─────────────────────────────────────
CLASSES = [
    (CUTOFF_MSG,     "EOD_SQUAREOFF_INTRADAY_CUTOFF",     "INTRADAY CUTOFF",       1),
    (RESTRICTED_MSG, "EOD_SQUAREOFF_INTRADAY_RESTRICTED", "SURVEILLANCE RESTRICTED", 1),
    (FUNDS_MSG,      "EOD_SQUAREOFF_INSUFFICIENT_FUNDS",  "INSUFFICIENT FUNDS",    1),
    (CIRCUIT_MSG,    "EOD_SQUAREOFF_CIRCUIT_LIMIT",       "CIRCUIT LIMIT",         1),
    (GENERIC_MSG,    "EOD_SQUAREOFF_FAILED",              "EOD SQUAREOFF FAILED",  3),  # unclassified => retried
]


class TestRunEodFailureHandling:
    @pytest.mark.parametrize("msg,prefix,header,attempts", CLASSES)
    def test_rejection_leaves_position_open_and_alerts(self, env, msg, prefix, header, attempts):
        db, b, sent = env
        p = mkpos(db, "FAILCO", capital=1_000.0)
        b.place_by_sec[p.dhan_security_id] = RuntimeError(msg)
        assert eod.run_eod_squareoff(db) == 0
        assert p.status == "OPEN"
        assert p.error_message.startswith(prefix)
        assert any(header in m for m in sent["critical"])
        assert len(b.of("place_order")) == attempts
        assert p.closed_at is None and p.dhan_exit_order_id is None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)   # nothing released
        assert lock_held(db, "FAILCO")                            # still protected from re-entry
        assert p.consecutive_exit_failures == 1
        assert fired_date(db) == TODAY                            # once-per-day guard: no auto-retry

    def test_restricted_symbol_is_recorded_for_future_screening(self, env):
        db, b, _ = env
        p = mkpos(db, "t2tco")
        b.place_by_sec[p.dhan_security_id] = RuntimeError(RESTRICTED_MSG)
        eod.run_eod_squareoff(db)
        row = db.query(models.ScalpIntradayRestrictedSecurity).one()
        assert row.symbol == "T2TCO"

    def test_restriction_recording_failure_is_swallowed(self, env, monkeypatch):
        db, b, _ = env
        p = mkpos(db)
        b.place_by_sec[p.dhan_security_id] = RuntimeError(RESTRICTED_MSG)

        def boom(*a, **k):
            raise RuntimeError("db hiccup")
        monkeypatch.setattr(eod.intraday_eligibility, "record_restriction", boom)
        assert eod.run_eod_squareoff(db) == 0
        assert p.error_message.startswith("EOD_SQUAREOFF_INTRADAY_RESTRICTED")

    @pytest.mark.parametrize("msg,prefix", [(m, pfx) for m, pfx, _, _ in CLASSES])
    def test_alert_channel_down_still_records_the_failure(self, env, msg, prefix):
        db, b, sent = env
        sent["boom"] = True
        p = mkpos(db)
        b.place_by_sec[p.dhan_security_id] = RuntimeError(msg)
        assert eod.run_eod_squareoff(db) == 0
        assert p.error_message.startswith(prefix)
        assert fired_date(db) == TODAY

    def test_one_failure_does_not_stop_the_rest(self, env):
        db, b, _ = env
        bad = mkpos(db, "BAD")
        good = mkpos(db, "GOOD")
        b.place_by_sec[bad.dhan_security_id] = RuntimeError(CIRCUIT_MSG)
        assert eod.run_eod_squareoff(db) == 1
        assert bad.status == "OPEN" and good.status == "EOD_SQUAREOFF"

    def test_position_in_cooldown_is_skipped_quietly(self, env):
        db, b, sent = env
        cool = mkpos(db, consecutive_exit_failures=3,
                     last_exit_failure_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        good = mkpos(db)
        assert eod.run_eod_squareoff(db) == 1
        assert cool.status == "OPEN" and cool.error_message is None
        assert good.status == "EOD_SQUAREOFF"
        assert [c["security_id"] for c in b.of("place_order")] == [good.dhan_security_id]
        assert sent["critical"] == []

    def test_cooldown_backoff_sequence_across_days(self, env):
        db, b, _ = env
        p = mkpos(db)
        b.place_by_sec[p.dhan_security_id] = RuntimeError(CIRCUIT_MSG)
        eod.run_eod_squareoff(db)
        assert p.consecutive_exit_failures == 1
        # next trading day: gate resets, but the 60s cooldown has long elapsed only if we say so
        p.last_exit_failure_at = datetime.now(timezone.utc) - timedelta(hours=20)
        eod._get_gate_state(db).eod_squareoff_fired_date = None
        db.commit()
        eod.run_eod_squareoff(db)
        assert p.consecutive_exit_failures == 2            # streak keeps growing until a placement succeeds


# ── Overnight carry ──────────────────────────────────────────────────────────
class TestOvernightCarry:
    @pytest.fixture(autouse=True)
    def _on(self, env, monkeypatch):
        enable_carry(monkeypatch)

    def test_qualifying_position_is_converted_and_protected_not_sold(self, env):
        db, b, sent = env
        p = qualifying(db, b, entry=100.0, qty=10)
        assert eod.run_eod_squareoff(db) == 0                       # nothing sold
        assert p.status == "OPEN"
        assert p.overnight_converted_to_cnc is True
        assert p.overnight_stop_order_id == "S1"
        conv = b.of("convert_position")[0]
        assert conv["position_type"] == "LONG" and conv["convert_qty"] == 10
        assert conv["from_product_type"] == "INTRADAY" and conv["to_product_type"] == "CNC"
        assert conv["security_id"] == p.dhan_security_id and conv["is_armed"] is True
        stop = b.of("place_cnc_stop_loss_market")[0]
        assert stop["trigger_price"] == pytest.approx(96.0) and stop["quantity"] == 10
        assert b.of("place_order") == []
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)     # capital stays deployed
        assert lock_held(db, p.symbol)
        assert any("overnight carry" in m for m in sent["info"])
        assert fired_date(db) == TODAY

    def test_super_order_target_and_stop_legs_cancelled_before_conversion(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
        p = qualifying(db, b, super_id="SO7")
        eod.run_eod_squareoff(db)
        seq = [(c[0], c[1].get("order_leg")) for c in b.calls
               if c[0] in ("cancel_super_order", "sleep", "convert_position")]
        assert seq == [("cancel_super_order", "TARGET_LEG"), ("cancel_super_order", "STOP_LOSS_LEG"),
                       ("sleep", None), ("convert_position", None)]
        assert p.dhan_super_order_id is None

    def test_leg_cancel_failure_does_not_block_conversion(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
        b.cancel_super_fail = True
        p = qualifying(db, b, super_id="SO7")
        eod.run_eod_squareoff(db)
        assert p.overnight_converted_to_cnc is True

    # ── carry filter ──
    def test_not_in_profit_is_sold(self, env):
        db, b, _ = env
        p = qualifying(db, b, entry=100.0, ltp=100.0)              # equal is NOT in profit
        assert eod.run_eod_squareoff(db) == 1
        assert p.status == "EOD_SQUAREOFF" and b.of("convert_position") == []
        assert b.of("place_order")[0]["product_type"] == "INTRADAY"

    def test_in_loss_is_sold(self, env):
        db, b, _ = env
        p = qualifying(db, b, entry=100.0, ltp=95.0)
        assert eod.run_eod_squareoff(db) == 1 and p.status == "EOD_SQUAREOFF"

    def test_no_live_price_is_sold(self, env):
        db, b, _ = env
        p = mkpos(db)
        mklog(db, p.symbol)                                        # quality fine, but no tick
        assert eod.run_eod_squareoff(db) == 1 and p.status == "EOD_SQUAREOFF"

    def test_tick_feed_error_is_sold(self, env):
        db, b, _ = env
        p = qualifying(db, b)
        b.tick_raises.add(p.symbol)
        assert eod.run_eod_squareoff(db) == 1 and p.status == "EOD_SQUAREOFF"

    def test_no_candidate_log_is_sold(self, env):
        db, b, _ = env
        p = mkpos(db)
        b.ticks[p.symbol] = 110.0
        assert eod.run_eod_squareoff(db) == 1 and p.status == "EOD_SQUAREOFF"

    def test_skipped_decision_log_does_not_count(self, env):
        db, b, _ = env
        p = mkpos(db)
        mklog(db, p.symbol, decision="SKIPPED")
        b.ticks[p.symbol] = 110.0
        assert eod.run_eod_squareoff(db) == 1 and p.status == "EOD_SQUAREOFF"

    def test_latest_entered_log_row_wins(self, env):
        db, b, _ = env
        p = mkpos(db)
        mklog(db, p.symbol, fund=90, tech=90, age_min=600)         # old, great
        mklog(db, p.symbol, fund=10, tech=90, age_min=1)           # newest, fails fundamentals
        b.ticks[p.symbol] = 110.0
        assert eod.run_eod_squareoff(db) == 1 and p.status == "EOD_SQUAREOFF"

    @pytest.mark.parametrize("fund,tech,mcap", [
        (59.9, 70, 5000), (70, 59.9, 5000), (70, 70, 1999.9),
        (None, 70, 5000), (70, None, 5000), (70, 70, None),
    ])
    def test_failing_or_unknown_quality_is_sold(self, env, fund, tech, mcap):
        db, b, _ = env
        p = qualifying(db, b, fund=fund, tech=tech, mcap=mcap)
        assert eod.run_eod_squareoff(db) == 1 and p.status == "EOD_SQUAREOFF"
        assert b.of("convert_position") == []

    def test_exactly_at_thresholds_is_carried(self, env):
        db, b, _ = env
        p = qualifying(db, b, fund=60.0, tech=60.0, mcap=2000.0)
        assert eod.run_eod_squareoff(db) == 0 and p.overnight_converted_to_cnc is True

    def test_exit_legs_rejected_is_always_sold_even_if_it_would_qualify(self, env):
        db, b, _ = env
        p = qualifying(db, b, status="EXIT_LEGS_REJECTED")
        assert eod.run_eod_squareoff(db) == 1
        assert p.status == "EOD_SQUAREOFF" and b.of("convert_position") == []

    def test_already_converted_position_is_sold_not_reconverted(self, env):
        db, b, _ = env
        p = qualifying(db, b, cnc=True, stop_id="S1")
        assert eod.run_eod_squareoff(db) == 1
        assert b.of("convert_position") == []
        assert b.of("cancel_cnc_stop_loss_order")[0]["order_id"] == "S1"
        assert b.of("place_order")[0]["product_type"] == "CNC"
        assert p.status == "EOD_SQUAREOFF"

    # ── pool-exposure cap ──
    def test_pool_cap_keeps_highest_quality_and_sells_the_rest(self, env):
        db, b, sent = env
        # cap = 30% of 100k = 30k; each position is 20k -> only one fits.
        low = qualifying(db, b, entry=100.0, qty=200, fund=70, tech=70)     # score 140, created FIRST
        high = qualifying(db, b, entry=100.0, qty=200, fund=90, tech=90)    # score 180
        assert eod.run_eod_squareoff(db) == 1
        assert high.overnight_converted_to_cnc is True and high.status == "OPEN"
        assert low.status == "EOD_SQUAREOFF" and low.overnight_converted_to_cnc is False
        assert any("pool-exposure cap" in m and low.symbol in m for m in sent["info"])

    def test_pool_cap_boundary_is_inclusive(self, env):
        db, b, _ = env
        a = qualifying(db, b, entry=100.0, qty=150)            # 15k
        c = qualifying(db, b, entry=100.0, qty=150)            # 15k -> exactly 30k
        assert eod.run_eod_squareoff(db) == 0
        assert a.overnight_converted_to_cnc and c.overnight_converted_to_cnc

    def test_unknown_pool_size_disables_the_cap__CURRENT_BEHAVIOUR(self, env):
        db, b, _ = env
        led = ledger._get_or_create(db)
        led.total_allocated_capital = 0.0
        db.commit()
        big = qualifying(db, b, entry=100.0, qty=5_000)        # 500k — would blow any cap
        assert eod.run_eod_squareoff(db) == 0
        assert big.overnight_converted_to_cnc is True          # `total_pool > 0` guard => no cap at all

    def test_zero_capital_risked_falls_back_to_entry_times_qty(self, env):
        db, b, _ = env
        p = qualifying(db, b, entry=100.0, qty=400, capital=0.0)   # 40k > 30k cap
        assert eod.run_eod_squareoff(db) == 1 and p.status == "EOD_SQUAREOFF"

    # ── conversion / stop failures ──
    def test_conversion_failure_falls_back_to_a_plain_intraday_sell(self, env):
        db, b, sent = env
        p = qualifying(db, b)
        b.convert_fail.add(p.dhan_security_id)
        assert eod.run_eod_squareoff(db) == 1
        assert p.status == "EOD_SQUAREOFF" and p.overnight_converted_to_cnc is False
        assert b.of("place_order")[0]["product_type"] == "INTRADAY"
        assert b.of("place_cnc_stop_loss_market") == []

    def test_mixed_batch_one_carried_one_sold(self, env):
        db, b, _ = env
        ok = qualifying(db, b)
        bad = qualifying(db, b)
        b.convert_fail.add(bad.dhan_security_id)
        assert eod.run_eod_squareoff(db) == 1                   # only the sold one is counted
        assert ok.overnight_converted_to_cnc and ok.status == "OPEN"
        assert bad.status == "EOD_SQUAREOFF"

    @pytest.mark.parametrize("script", [[RuntimeError("stop rejected")], [{}]])
    def test_stop_failure_after_conversion_squares_off_as_cnc(self, env, script):
        db, b, sent = env
        b.stop_script = list(script)
        b.edis = {"verified_today": False, "pending_symbols": ["XYZ"]}
        p = qualifying(db, b)
        assert eod.run_eod_squareoff(db) == 1
        assert p.status == "EOD_SQUAREOFF"
        assert b.of("place_order")[0]["product_type"] == "CNC"    # it IS a CNC holding by now
        assert any("squared off because protective stop placement failed" in m and p.symbol in m
                   for m in sent["info"])
        assert any("eDIS" in m for m in sent["critical"])          # TPIN warning fires before the CNC SELL

    def test_stop_pct_zero_carries_without_a_stop_and_says_so(self, env, monkeypatch):
        db, b, sent = env
        monkeypatch.setattr(config, "OVERNIGHT_STOP_LOSS_PCT", 0.0)
        p = qualifying(db, b)
        assert eod.run_eod_squareoff(db) == 0
        assert p.overnight_converted_to_cnc is True and p.overnight_stop_order_id is None
        assert b.of("place_cnc_stop_loss_market") == []
        assert any("NO STOP" in m for m in sent["info"])

    def test_edis_precheck_is_skipped_when_nothing_is_cnc(self, env):
        db, b, _ = env
        qualifying(db, b, ltp=90.0)                                # sold as plain INTRADAY
        eod.run_eod_squareoff(db)
        assert b.of("edis_verification_summary") == []


# ── close_position_now ───────────────────────────────────────────────────────
class TestCloseNow:
    @pytest.mark.parametrize("status", ["CLOSED", "TARGET_HIT", "STOP_HIT", "EOD_SQUAREOFF", "MANUAL_EXIT", "ERROR"])
    def test_non_closeable_status_is_rejected_without_touching_the_broker(self, env, status):
        db, b, _ = env
        p = mkpos(db, status=status)
        with pytest.raises(eod.ManualCloseRejected, match="nothing to close"):
            eod.close_position_now(db, p)
        assert b.calls == []

    def test_open_position_is_closed_with_placeholder_and_side_effects(self, env):
        db, b, sent = env
        p = mkpos(db, "MANCO", entry=100.0, qty=10)
        res = eod.close_position_now(db, p)
        assert res == {"id": p.id, "symbol": "MANCO", "status": "pending_broker_confirmation"}
        assert p.status == "MANUAL_EXIT"
        assert p.exit_price == p.entry_price and p.realized_pnl == 0.0
        assert p.error_message.startswith("MANUAL_EXIT_PENDING_RECONCILE")
        assert p.dhan_exit_order_id == "F1" and p.closed_at is not None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 1_000.0)
        assert not lock_held(db, "MANCO")
        assert db.query(models.SharedOrderBudget).one().orders_placed_today == 1
        assert any("Manual EXIT sent" in m and "MANCO" in m for m in sent["info"])

    def test_exit_legs_rejected_can_be_closed(self, env):
        db, b, _ = env
        p = mkpos(db, status="EXIT_LEGS_REJECTED")
        eod.close_position_now(db, p)
        assert p.status == "MANUAL_EXIT"

    def test_stagnation_reason_becomes_the_real_status(self, env):
        db, b, sent = env
        p = mkpos(db)
        eod.close_position_now(db, p, exit_reason="STAGNATION_EXIT")
        assert p.status == "STAGNATION_EXIT"
        assert p.error_message.startswith("STAGNATION_EXIT_PENDING_RECONCILE")
        assert any("Stagnation Exit sent" in m for m in sent["info"])

    def test_super_order_legs_cancelled_then_pause_then_sell(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
        eod.close_position_now(db, mkpos(db, super_id="SO2"))
        seq = [(c[0], c[1].get("order_leg")) for c in b.calls
               if c[0] in ("cancel_super_order", "sleep", "place_order")]
        assert seq == [("cancel_super_order", "ENTRY_LEG"), ("cancel_super_order", "TARGET_LEG"),
                       ("cancel_super_order", "STOP_LOSS_LEG"), ("sleep", None), ("place_order", None)]

    def test_leg_cancel_failure_does_not_block_the_close(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
        b.cancel_super_fail = True
        p = mkpos(db, super_id="SO2")
        eod.close_position_now(db, p)
        assert p.status == "MANUAL_EXIT"

    def test_partial_pnl_from_overnight_stop_is_kept(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, overnight_stop_filled_qty_so_far=30, realized_pnl=-120.0, realized_pnl_pct=-1.2)
        eod.close_position_now(db, p)
        assert p.realized_pnl == pytest.approx(-120.0)

    @pytest.mark.parametrize("msg,expected", [
        (CUTOFF_MSG, "intraday order window has closed"),
        (RESTRICTED_MSG, "not tradeable intraday"),
        (FUNDS_MSG, "insufficient margin"),
        (CIRCUIT_MSG, "circuit limit"),
        (GENERIC_MSG, "rejected the manual exit"),
    ])
    def test_rejections_are_translated_and_leave_the_position_untouched(self, env, msg, expected):
        db, b, _ = env
        p = mkpos(db, "REJCO")
        b.place_by_sec[p.dhan_security_id] = RuntimeError(msg)
        with pytest.raises(eod.ManualCloseRejected, match=expected):
            eod.close_position_now(db, p)
        assert p.status == "OPEN" and p.closed_at is None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)
        assert lock_held(db, "REJCO")

    def test_restricted_rejection_records_the_symbol(self, env):
        db, b, _ = env
        p = mkpos(db, "t2tco")
        b.place_by_sec[p.dhan_security_id] = RuntimeError(RESTRICTED_MSG)
        with pytest.raises(eod.ManualCloseRejected):
            eod.close_position_now(db, p)
        assert db.query(models.ScalpIntradayRestrictedSecurity).one().symbol == "T2TCO"

    def test_restriction_recording_failure_is_swallowed(self, env, monkeypatch):
        db, b, _ = env
        p = mkpos(db)
        b.place_by_sec[p.dhan_security_id] = RuntimeError(RESTRICTED_MSG)
        monkeypatch.setattr(eod.intraday_eligibility, "record_restriction",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
        with pytest.raises(eod.ManualCloseRejected, match="not tradeable intraday"):
            eod.close_position_now(db, p)

    def test_already_flat_via_overnight_stop_is_reported(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, stop_id="S1", qty=10)
        b.orders = [order_row("S1", "TRADED", 10, 96.0)]
        with pytest.raises(eod.ManualCloseRejected, match="Already closed"):
            eod.close_position_now(db, p)

    def test_cooldown_is_reported_as_a_rejection(self, env):
        db, b, _ = env
        p = mkpos(db, consecutive_exit_failures=2,
                  last_exit_failure_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        with pytest.raises(eod.ManualCloseRejected, match="cooldown"):
            eod.close_position_now(db, p)
        assert b.of("place_order") == []


# ── run_stagnation_exit ──────────────────────────────────────────────────────
class TestStagnationExit:
    @pytest.fixture(autouse=True)
    def _enabled(self, env):
        db, _, _ = env
        g = eod._get_gate_state(db)
        g.stagnation_exit_enabled = True
        db.commit()

    def test_disabled_toggle_does_nothing(self, env):
        db, b, _ = env
        g = eod._get_gate_state(db)
        g.stagnation_exit_enabled = False
        db.commit()
        p = mkpos(db, opened_min_ago=600)
        b.ticks[p.symbol] = p.entry_price
        assert eod.run_stagnation_exit(db) == 0
        assert b.calls == [] and p.status == "OPEN"

    def test_old_flat_position_is_closed_as_stagnation_exit(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, opened_min_ago=60)
        b.ticks[p.symbol] = 100.2                                   # +0.2% < 0.35% band
        assert eod.run_stagnation_exit(db) == 1
        assert p.status == "STAGNATION_EXIT"

    def test_downward_drift_inside_band_also_counts(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, opened_min_ago=60)
        b.ticks[p.symbol] = 99.8
        assert eod.run_stagnation_exit(db) == 1

    def test_too_young_is_left_alone(self, env):
        db, b, _ = env
        p = mkpos(db, opened_min_ago=44)
        b.ticks[p.symbol] = p.entry_price
        assert eod.run_stagnation_exit(db) == 0 and p.status == "OPEN"

    def test_moved_beyond_band_is_left_alone(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, opened_min_ago=60)
        b.ticks[p.symbol] = 100.5
        assert eod.run_stagnation_exit(db) == 0

    def test_move_exactly_at_band_is_left_alone(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "STAGNATION_EXIT_BAND_PCT", 0.5)
        p = mkpos(db, entry=100.0, opened_min_ago=60)
        b.ticks[p.symbol] = 100.5                                   # exactly 0.5%
        assert eod.run_stagnation_exit(db) == 0

    @pytest.mark.parametrize("ltp", [None, 0.0, -1.0])
    def test_no_usable_live_price_is_left_alone(self, env, ltp):
        db, b, _ = env
        p = mkpos(db, opened_min_ago=60)
        if ltp is not None:
            b.ticks[p.symbol] = ltp
        assert eod.run_stagnation_exit(db) == 0 and p.status == "OPEN"

    def test_tick_feed_error_is_left_alone(self, env):
        db, b, _ = env
        p = mkpos(db, opened_min_ago=60)
        b.tick_raises.add(p.symbol)
        assert eod.run_stagnation_exit(db) == 0

    def test_zero_entry_price_is_left_alone(self, env):
        db, b, _ = env
        p = mkpos(db, entry=0.0, capital=100.0, opened_min_ago=60)
        b.ticks[p.symbol] = 1.0
        assert eod.run_stagnation_exit(db) == 0

    def test_overnight_carried_position_is_never_stagnation_closed(self, env):
        db, b, _ = env
        p = mkpos(db, cnc=True, stop_id="S1", opened_min_ago=60 * 24)
        b.ticks[p.symbol] = p.entry_price
        assert eod.run_stagnation_exit(db) == 0 and p.status == "OPEN"
        assert b.of("place_order") == []

    def test_exit_legs_rejected_is_not_this_jobs_business(self, env):
        db, b, _ = env
        p = mkpos(db, status="EXIT_LEGS_REJECTED", opened_min_ago=60)
        b.ticks[p.symbol] = p.entry_price
        assert eod.run_stagnation_exit(db) == 0 and p.status == "EXIT_LEGS_REJECTED"

    def test_broker_rejection_skips_that_position_and_continues(self, env):
        db, b, _ = env
        bad = mkpos(db, opened_min_ago=60)
        good = mkpos(db, opened_min_ago=60)
        for p in (bad, good):
            b.ticks[p.symbol] = p.entry_price
        b.place_by_sec[bad.dhan_security_id] = RuntimeError(CIRCUIT_MSG)
        assert eod.run_stagnation_exit(db) == 1
        assert bad.status == "OPEN" and good.status == "STAGNATION_EXIT"

    def test_unexpected_error_skips_that_position_and_continues(self, env, monkeypatch):
        db, b, _ = env
        bad = mkpos(db, opened_min_ago=60)
        good = mkpos(db, opened_min_ago=60)
        for p in (bad, good):
            b.ticks[p.symbol] = p.entry_price
        real = eod.close_position_now

        def flaky(db_, pos, exit_reason="MANUAL_EXIT"):
            if pos.id == bad.id:
                raise ValueError("unexpected")
            return real(db_, pos, exit_reason=exit_reason)
        monkeypatch.setattr(eod, "close_position_now", flaky)
        assert eod.run_stagnation_exit(db) == 1
        assert good.status == "STAGNATION_EXIT" and bad.status == "OPEN"

    def test_naive_opened_at_from_the_db_is_treated_as_utc(self, env):
        db, b, _ = env
        p = mkpos(db, opened_min_ago=60)
        db.expire_all()                                             # force a naive datetime read-back
        assert p.opened_at.tzinfo is None
        b.ticks[p.symbol] = p.entry_price
        assert eod.run_stagnation_exit(db) == 1


# ── exit_retry ───────────────────────────────────────────────────────────────
class TestExitRetry:
    def test_no_failures_means_no_cooldown(self, env):
        db, _, _ = env
        assert exit_retry.cooldown_remaining_seconds(mkpos(db)) == 0.0

    @pytest.mark.parametrize("failures,window", [(1, 60), (2, 120), (3, 240), (4, 480), (5, 900), (20, 900)])
    def test_exponential_window_capped_at_max(self, env, failures, window):
        db, _, _ = env
        ago = 30
        p = mkpos(db, consecutive_exit_failures=failures,
                  last_exit_failure_at=datetime.now(timezone.utc) - timedelta(seconds=ago))
        assert exit_retry.cooldown_remaining_seconds(p) == pytest.approx(window - ago, abs=1.0)

    def test_expired_window_is_zero_not_negative(self, env):
        db, _, _ = env
        p = mkpos(db, consecutive_exit_failures=1,
                  last_exit_failure_at=datetime.now(timezone.utc) - timedelta(hours=5))
        assert exit_retry.cooldown_remaining_seconds(p) == 0.0
        exit_retry.check_cooldown(p)                                # must not raise

    def test_failure_count_without_timestamp_is_not_a_cooldown(self, env):
        db, _, _ = env
        assert exit_retry.cooldown_remaining_seconds(mkpos(db, consecutive_exit_failures=4)) == 0.0

    def test_record_failure_increments_and_stamps(self, env):
        db, _, sent = env
        p = mkpos(db)
        exit_retry.record_failure(db, p, "boom")
        exit_retry.record_failure(db, p, "boom")
        assert p.consecutive_exit_failures == 2 and p.last_exit_failure_at is not None
        assert sent["critical"] == []                               # below the alert threshold

    def test_alert_fires_at_threshold_and_every_multiple(self, env):
        db, _, sent = env
        p = mkpos(db)
        for _ in range(4):
            exit_retry.record_failure(db, p, "boom")
        assert sent["critical"] == []
        exit_retry.record_failure(db, p, "boom")                    # 5th
        assert len(sent["critical"]) == 1 and "Exit SELL placement stuck" in sent["critical"][0]
        for _ in range(4):
            exit_retry.record_failure(db, p, "boom")
        assert len(sent["critical"]) == 1                           # 6th-9th: quiet
        exit_retry.record_failure(db, p, "boom")                    # 10th
        assert len(sent["critical"]) == 2

    def test_alert_disabled_when_threshold_is_zero(self, env, monkeypatch):
        db, _, sent = env
        monkeypatch.setattr(config, "EXIT_RETRY_ALERT_THRESHOLD", 0)
        p = mkpos(db)
        for _ in range(10):
            exit_retry.record_failure(db, p, "boom")
        assert sent["critical"] == []

    def test_alert_channel_failure_is_swallowed(self, env):
        db, _, sent = env
        sent["boom"] = True
        p = mkpos(db, consecutive_exit_failures=4)
        exit_retry.record_failure(db, p, "boom")                    # 5th -> tries to alert, must not raise
        assert p.consecutive_exit_failures == 5

    def test_reset_clears_streak_and_is_safe_when_already_clean(self, env):
        db, _, _ = env
        p = mkpos(db, consecutive_exit_failures=3, last_exit_failure_at=datetime.now(timezone.utc))
        exit_retry.reset(p)
        assert p.consecutive_exit_failures == 0 and p.last_exit_failure_at is None
        exit_retry.reset(p)
        assert p.consecutive_exit_failures == 0
