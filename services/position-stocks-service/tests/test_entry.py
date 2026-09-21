"""
tests/test_entry.py — offline tests for orders/entry.py (position-stocks-service).

This is the code that spends real money: every automatic and manual BUY goes
through the gates below. The tests pin down, for each gate, (a) that it rejects
what it should, (b) that it accepts the boundary case, and (c) that a rejected
entry leaves NOTHING behind — capital returned to the ledger, cross-service
symbol lock released, no position row, no broker order.

Everything runs offline: real models on in-memory SQLite (real ledger, symbol
lock and order-budget code), a scripted fake broker patched onto
`execution.dhan_client`, and `compute_levels` replaced by a fixed function so
sizing is exact and predictable:

    total pool ₹100,000, RISK_PER_TRADE_PCT 2%, 5 slots, stop 2%
    => position_value = 100,000 * 2% / 5 / 2% = ₹20,000

Sections:
  TestHelpers           _range_gate_reject, _reentry_guard_reject, gate/count helpers
  TestAttemptEntryHappy the successful auto BUY, field by field
  TestAttemptEntrySkips every pre-order rejection
  TestAttemptEntrySizing min-qty shortfall, first-live-order override
  TestAttemptEntryOrderFailures the five broker-rejection classes
  TestManualEntry       attempt_manual_entry()
  TestAuditRegressions  the leaks/omissions found while writing these — now fixed

Run from services/position-stocks-service:
    python3 -m pytest tests/test_entry.py -q --cov=orders --cov-report=term-missing
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
from orders import entry
from orders.adaptive import AdaptiveLevels
from screening.engine import Candidate
from screening.quality_gate import QualitySignal

TODAY = "2026-09-21"
LEDGER_TOTAL = 100_000.0
LEDGER_AVAILABLE = 50_000.0
POSITION_VALUE = 20_000.0          # see module docstring

RESTRICTED_MSG = "Scrip is not allowed to be traded in Intraday"
CUTOFF_MSG = "Intraday orders cannot be placed at this time"
FUNDS_MSG = "RMS: Insufficient funds. Add Rs. 5000"
CIRCUIT_MSG = "RMS: Rate Not Within Ckt Limit 395.25 To 592.85"
GENERIC_MSG = "gateway timeout talking to broker"


class Broker:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.sec_error: BaseException | None = None
        self.super_script: list = []
        self.plain_script: list = []
        self.ticks: dict[str, list[float]] = {}
        self.tick_raises: set[str] = set()
        self.stop_pct = 2.0
        self._n = 0

    def of(self, name):
        return [c[1] for c in self.calls if c[0] == name]


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
    sent = {"info": []}
    monkeypatch.setattr(notifier, "notify_sync", lambda m, *a, **k: sent["info"].append(m) or True)

    def _sec(db_, symbol):
        b.calls.append(("get_security_id", {"symbol": symbol}))
        if b.sec_error:
            raise b.sec_error
        return f"SEC_{symbol}"

    def _super(db_, **kw):
        b.calls.append(("place_super_order", kw))
        b._n += 1
        out = b.super_script.pop(0) if b.super_script else None
        return _outcome(out, {"orderId": f"SO{b._n}"})

    def _plain(db_, **kw):
        b.calls.append(("place_order", kw))
        b._n += 1
        out = b.plain_script.pop(0) if b.plain_script else None
        return _outcome(out, {"orderId": f"PO{b._n}"})

    monkeypatch.setattr(dhan_client, "get_security_id", _sec)
    monkeypatch.setattr(dhan_client, "place_super_order", _super)
    monkeypatch.setattr(dhan_client, "place_order", _plain)

    def _levels(pct_change, ltp, symbol=None):
        b.calls.append(("compute_levels", {"pct_change": pct_change, "ltp": ltp, "symbol": symbol}))
        s = b.stop_pct
        return AdaptiveLevels(
            target_pct=1.0, stop_pct=s, target_price=round(ltp * 1.01, 2),
            stop_price=round(ltp * (1 - s / 100.0), 2), breakeven_trigger_pct=0.5,
        )
    monkeypatch.setattr(entry, "compute_levels", _levels)

    def _tick_buffer(sym):
        if sym in b.tick_raises:
            raise RuntimeError("ws down")
        return [(i, p) for i, p in enumerate(b.ticks.get(sym, []))]
    monkeypatch.setattr(ws_client, "get_tick_buffer", _tick_buffer)
    monkeypatch.setattr(entry, "ist_today_str", lambda: TODAY)

    pins = dict(
        DAILY_ORDER_BUDGET=300, SHARED_DAILY_ORDER_BUDGET=5000, MAX_CONCURRENT_SCALP_POSITIONS=5,
        MIN_STOCK_PRICE=20.0, MIN_TICKS_FOR_RANGE_GATE=10, MAX_ENTRY_RANGE_POSITION=0.92,
        SYMBOL_REENTRY_COOLDOWN_MINUTES=30, SYMBOL_REENTRY_MIN_PULLBACK_PCT=1.0,
        FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE=False, USE_SUPER_ORDER=True,
        SCALP_PRODUCT_TYPE="INTRADAY", SCALP_EXCHANGE_SEGMENT="NSE_EQ",
        RISK_PER_TRADE_PCT=2.0, MAX_DAILY_LOSS_PCT_OF_POOL=4.0,
    )
    for k, v in pins.items():
        monkeypatch.setattr(config, k, v)

    led = ledger._get_or_create(db)
    led.total_allocated_capital, led.available_capital = LEDGER_TOTAL, LEDGER_AVAILABLE
    gate = entry._get_gate_state(db)
    gate.is_armed = True
    gate.first_live_order_done = True          # most tests are not "the very first live order"
    db.commit()
    yield db, b, sent
    db.close()


# ── Builders / readers ───────────────────────────────────────────────────────
_ids = itertools.count(1)


def cand(symbol="ABC", ltp=500.0, pct=1.5, window=5, score=0.7):
    return Candidate(symbol=symbol, window_minutes=window, pct_change=pct, current_ltp=ltp,
                     tick_activity=100, composite_score=score)


def mkpos(db, symbol=None, *, status="OPEN", entry_price=100.0, qty=10, exit_price=None,
          closed_min_ago=None, claim_lock=False):
    n = next(_ids)
    symbol = symbol or f"SYM{n}"
    p = models.ScalpPosition(
        symbol=symbol, dhan_security_id=str(1000 + n), window_source="5m", status=status,
        entry_price=entry_price, quantity=qty, target_price=entry_price * 1.02,
        stop_price=entry_price * 0.99, adaptive_target_pct=2.0, adaptive_stop_pct=1.0,
        capital_risked=entry_price * qty, exit_price=exit_price,
        closed_at=(datetime.now(timezone.utc) - timedelta(minutes=closed_min_ago)
                   if closed_min_ago is not None else None),
    )
    db.add(p)
    db.commit()
    if claim_lock:
        shared_symbol_lock.try_claim(db, symbol)
    return p


def gate_of(db):
    return db.query(models.ScalpGateState).filter_by(mode="REAL").first()


def available(db) -> float:
    return ledger.get_state(db)["available_capital"]


def lock_held(db, symbol) -> bool:
    return db.query(models.SharedSymbolLock).filter_by(symbol=symbol.upper()).first() is not None


def last_log(db):
    return db.query(models.ScalpCandidateLog).order_by(models.ScalpCandidateLog.id.desc()).first()


def n_positions(db) -> int:
    return db.query(models.ScalpPosition).count()


def assert_clean_skip(env_, symbol, reason_prefix, *, capital=LEDGER_AVAILABLE):
    """A rejected entry must leave no trace except one SKIPPED log row."""
    db, b, _ = env_
    log = last_log(db)
    assert log.decision == "SKIPPED" and log.reason.startswith(reason_prefix), (log.decision, log.reason)
    assert n_positions(db) == 0
    assert available(db) == pytest.approx(capital)
    assert not lock_held(db, symbol)
    assert b.of("place_super_order") == [] and b.of("place_order") == []


# ── Helpers ──────────────────────────────────────────────────────────────────
class TestHelpers:
    # _range_gate_reject
    def _ticks(self, b, prices):
        b.ticks["ABC"] = prices

    WIDE = [100.0, 200.0] + [150.0] * 8                       # 10 ticks, low 100 high 200

    def test_range_gate_rejects_at_the_high(self, env):
        db, b, _ = env
        self._ticks(b, self.WIDE)
        r = entry._range_gate_reject("ABC", 200.0)
        assert r.startswith("NEAR_DAY_HIGH:") and "day_low=100.00" in r and "day_high=200.00" in r

    def test_range_gate_boundary_is_inclusive(self, env):
        db, b, _ = env
        self._ticks(b, self.WIDE)
        assert entry._range_gate_reject("ABC", 192.0) is not None     # exactly 0.92
        assert entry._range_gate_reject("ABC", 191.0) is None         # 0.91

    def test_range_gate_needs_enough_ticks(self, env):
        db, b, _ = env
        self._ticks(b, self.WIDE[:9])                                 # 9 < 10
        assert entry._range_gate_reject("ABC", 200.0) is None

    def test_range_gate_ignores_non_positive_prices_when_counting(self, env):
        db, b, _ = env
        self._ticks(b, [100.0, 200.0] + [150.0] * 7 + [0.0, -1.0])    # only 9 usable
        assert entry._range_gate_reject("ABC", 200.0) is None

    def test_range_gate_flat_range_fails_open(self, env):
        db, b, _ = env
        self._ticks(b, [100.0] * 12)
        assert entry._range_gate_reject("ABC", 100.0) is None

    def test_range_gate_ltp_above_day_high_clamps_and_rejects(self, env):
        db, b, _ = env
        self._ticks(b, self.WIDE)
        assert entry._range_gate_reject("ABC", 999.0) is not None

    def test_range_gate_feed_error_fails_open(self, env):
        db, b, _ = env
        self._ticks(b, self.WIDE)
        b.tick_raises.add("ABC")
        assert entry._range_gate_reject("ABC", 200.0) is None

    # _reentry_guard_reject
    def test_reentry_no_history_allows(self, env):
        db, _, _ = env
        assert entry._reentry_guard_reject(db, "ABC", 500.0) is None

    def test_reentry_blocks_chasing_a_just_sold_symbol(self, env):
        db, _, _ = env
        mkpos(db, "ABC", status="TARGET_HIT", exit_price=500.0, closed_min_ago=5)
        r = entry._reentry_guard_reject(db, "ABC", 500.0)
        assert r.startswith("REENTRY_COOLDOWN:") and "TARGET_HIT" in r

    def test_reentry_allows_a_real_pullback(self, env):
        db, _, _ = env
        mkpos(db, "ABC", status="TARGET_HIT", exit_price=500.0, closed_min_ago=5)
        assert entry._reentry_guard_reject(db, "ABC", 495.0) is None      # exactly -1.0%: allowed
        assert entry._reentry_guard_reject(db, "ABC", 495.01) is not None # a hair above: blocked

    def test_reentry_cooldown_boundary(self, env):
        db, _, _ = env
        mkpos(db, "ABC", status="STOP_HIT", exit_price=500.0, closed_min_ago=31)
        assert entry._reentry_guard_reject(db, "ABC", 500.0) is None      # 31m >= 30m
        mkpos(db, "DEF", status="STOP_HIT", exit_price=500.0, closed_min_ago=29)
        assert entry._reentry_guard_reject(db, "DEF", 500.0) is not None

    def test_reentry_uses_the_most_recent_close(self, env):
        db, _, _ = env
        mkpos(db, "ABC", status="STOP_HIT", exit_price=300.0, closed_min_ago=200)
        mkpos(db, "ABC", status="STOP_HIT", exit_price=500.0, closed_min_ago=5)
        assert entry._reentry_guard_reject(db, "ABC", 500.0) is not None

    def test_reentry_ignores_other_symbols(self, env):
        db, _, _ = env
        mkpos(db, "OTHER", status="TARGET_HIT", exit_price=500.0, closed_min_ago=1)
        assert entry._reentry_guard_reject(db, "ABC", 500.0) is None

    @pytest.mark.parametrize("status", ["OPEN", "EXIT_LEGS_REJECTED"])
    def test_reentry_ignores_still_open_positions(self, env, status):
        # each on its own: with another row present the "latest close" could mask a wrong filter
        db, _, _ = env
        mkpos(db, "ABC", status=status, closed_min_ago=1, exit_price=500.0)
        assert entry._reentry_guard_reject(db, "ABC", 500.0) is None

    def test_reentry_ignores_a_close_with_no_exit_price(self, env):
        db, _, _ = env
        mkpos(db, "ABC", status="ERROR", exit_price=None, closed_min_ago=1)
        assert entry._reentry_guard_reject(db, "ABC", 500.0) is None

    # gate / counters / logging
    def test_gate_row_created_and_kill_switch_lazily_reset(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.daily_loss_kill_switch_tripped, g.daily_loss_kill_switch_tripped_date = True, "2026-09-18"
        db.commit()
        g = entry._get_gate_state(db)
        assert g.daily_loss_kill_switch_tripped is False and g.daily_loss_kill_switch_tripped_date is None

    def test_gate_kill_switch_kept_same_day(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.daily_loss_kill_switch_tripped, g.daily_loss_kill_switch_tripped_date = True, TODAY
        db.commit()
        assert entry._get_gate_state(db).daily_loss_kill_switch_tripped is True

    def test_open_count_includes_exit_legs_rejected_only(self, env):
        db, _, _ = env
        for s in ("OPEN", "OPEN", "EXIT_LEGS_REJECTED", "TARGET_HIT", "STOP_HIT", "ERROR", "EOD_SQUAREOFF"):
            mkpos(db, status=s)
        assert entry._count_open_positions(db) == 3

    def test_log_quality_reject_writes_a_skipped_row_with_scores(self, env):
        db, _, _ = env
        q = QualitySignal(symbol="ABC", fundamental_score=40.0, technical_score=50.0,
                          market_cap_cr=900.0, has_positive_catalyst=False)
        entry.log_quality_reject(db, cand(), q, "fundamental_score 40 < floor 60")
        log = last_log(db)
        assert log.decision == "SKIPPED" and log.reason == "QUALITY_GATE:fundamental_score 40 < floor 60"
        assert (log.fundamental_score, log.technical_score, log.market_cap_cr) == (40.0, 50.0, 900.0)
        assert log.has_positive_catalyst is False
        assert (log.symbol, log.window_source, log.pct_change) == ("ABC", "5m", 1.5)

    def test_log_row_without_quality_has_null_scores(self, env):
        db, _, _ = env
        entry._log_candidate(db, cand(score=0.7), "SKIPPED", "X")
        log = last_log(db)
        assert log.fundamental_score is None and log.composite_score == pytest.approx(0.7)


# ── attempt_entry: success ───────────────────────────────────────────────────
class TestAttemptEntryHappy:
    def test_successful_super_order_entry(self, env):
        db, b, sent = env
        q = QualitySignal(symbol="ABC", fundamental_score=70.0, technical_score=65.0,
                          market_cap_cr=8000.0, has_positive_catalyst=True)
        pos = entry.attempt_entry(db, cand(ltp=500.0), quality=q)

        assert pos is not None and pos.id is not None
        assert (pos.symbol, pos.status, pos.window_source) == ("ABC", "OPEN", "5m")
        assert pos.quantity == 40 and pos.entry_price == 500.0
        assert pos.capital_risked == pytest.approx(POSITION_VALUE)
        assert pos.dhan_security_id == "SEC_ABC"
        assert pos.dhan_super_order_id == "SO1" and pos.dhan_entry_order_id == "SO1"
        assert pos.target_price == pytest.approx(505.0) and pos.stop_price == pytest.approx(490.0)
        assert pos.adaptive_target_pct == 1.0 and pos.adaptive_stop_pct == 2.0
        assert pos.breakeven_trigger_pct == 0.5
        assert pos.is_first_live_order is False and pos.opened_at is not None

        kw = b.of("place_super_order")[0]
        assert kw["is_armed"] is True and kw["security_id"] == "SEC_ABC"
        assert kw["exchange_segment"] == "NSE_EQ" and kw["transaction_type"] == "BUY"
        assert kw["quantity"] == 40 and kw["order_type"] == "MARKET" and kw["price"] == 500.0
        assert kw["target_price"] == pytest.approx(505.0) and kw["stop_loss_price"] == pytest.approx(490.0)
        assert kw["trailing_jump"] == 0.0 and kw["product_type"] == "INTRADAY" and kw["tag"] == "SCALP"
        assert b.of("place_order") == []
        assert b.of("compute_levels")[0] == {"pct_change": 1.5, "ltp": 500.0, "symbol": "ABC"}

        assert available(db) == pytest.approx(LEDGER_AVAILABLE - POSITION_VALUE)
        assert lock_held(db, "ABC")
        g = gate_of(db)
        assert g.orders_placed_today == 1 and g.orders_placed_today_date == TODAY
        assert db.query(models.SharedOrderBudget).one().orders_placed_today == 1

        log = last_log(db)
        assert log.decision == "ENTERED" and log.reason == "SUPER_ORDER=SO1"
        assert (log.fundamental_score, log.technical_score, log.market_cap_cr) == (70.0, 65.0, 8000.0)
        assert log.has_positive_catalyst is True
        assert any("BUY placed" in m and "ABC" in m and "x40" in m for m in sent["info"])

    def test_alternate_order_id_key_is_accepted(self, env):
        db, b, _ = env
        b.super_script = [{"id": "ALT1"}]
        assert entry.attempt_entry(db, cand()).dhan_super_order_id == "ALT1"

    def test_missing_order_id_is_recorded_as_blank__CURRENT_BEHAVIOUR(self, env):
        # A broker "success" that carries no id is still recorded as an OPEN
        # position, with a blank id and a log line that says plain_order.
        db, b, _ = env
        b.super_script = [{}]
        pos = entry.attempt_entry(db, cand())
        assert pos is not None and pos.dhan_super_order_id == ""
        assert last_log(db).reason == "SUPER_ORDER=plain_order"

    def test_plain_order_fallback_when_super_orders_disabled(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", False)
        pos = entry.attempt_entry(db, cand())
        kw = b.of("place_order")[0]
        assert b.of("place_super_order") == []
        assert kw["price"] == 0.0 and kw["order_type"] == "MARKET" and kw["tag"] == "SCALP"
        assert kw["transaction_type"] == "BUY" and kw["quantity"] == 40
        assert pos.dhan_super_order_id is None
        assert pos.dhan_entry_order_id == "PO1"                       # the BUY id is kept for tracing
        assert last_log(db).reason == "SUPER_ORDER=plain_order"

    def test_second_entry_same_day_increments_counters(self, env):
        db, b, _ = env
        entry.attempt_entry(db, cand("AAA"))
        entry.attempt_entry(db, cand("BBB"))
        assert gate_of(db).orders_placed_today == 2
        assert db.query(models.SharedOrderBudget).one().orders_placed_today == 2

    def test_stale_daily_counter_resets_on_a_new_day(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.orders_placed_today, g.orders_placed_today_date = 299, "2026-09-18"
        db.commit()
        assert entry.attempt_entry(db, cand()) is not None          # yesterday's 299 must not block
        g = gate_of(db)
        assert g.orders_placed_today == 1 and g.orders_placed_today_date == TODAY


# ── attempt_entry: rejections before any order ───────────────────────────────
class TestAttemptEntrySkips:
    def test_not_armed(self, env):
        db, b, _ = env
        g = gate_of(db)
        g.is_armed = False
        db.commit()
        assert entry.attempt_entry(db, cand()) is None
        assert_clean_skip(env, "ABC", "SERVICE_NOT_ARMED")
        assert b.of("get_security_id") == []

    def test_kill_switch_tripped(self, env):
        db, b, _ = env
        g = gate_of(db)
        g.daily_loss_kill_switch_tripped, g.daily_loss_kill_switch_tripped_date = True, TODAY
        db.commit()
        assert entry.attempt_entry(db, cand()) is None
        assert_clean_skip(env, "ABC", "DAILY_LOSS_KILL_SWITCH")

    def test_kill_switch_from_an_earlier_day_no_longer_blocks(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.daily_loss_kill_switch_tripped, g.daily_loss_kill_switch_tripped_date = True, "2026-09-18"
        db.commit()
        assert entry.attempt_entry(db, cand()) is not None

    def test_order_budget_exhausted_today(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.orders_placed_today, g.orders_placed_today_date = 300, TODAY
        db.commit()
        assert entry.attempt_entry(db, cand()) is None
        assert_clean_skip(env, "ABC", "ORDER_BUDGET_EXHAUSTED:300")

    def test_yesterdays_exhausted_budget_does_not_block_today(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.orders_placed_today, g.orders_placed_today_date = 300, "2026-09-18"
        db.commit()
        assert entry.attempt_entry(db, cand()) is not None

    def test_order_budget_one_below_cap_allows(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.orders_placed_today, g.orders_placed_today_date = 299, TODAY
        db.commit()
        assert entry.attempt_entry(db, cand()) is not None

    def test_max_positions_reached(self, env):
        db, _, _ = env
        for _ in range(4):
            mkpos(db, status="OPEN")
        mkpos(db, status="EXIT_LEGS_REJECTED")                       # stuck positions still count
        for s in ("TARGET_HIT", "STOP_HIT", "ERROR"):
            mkpos(db, status=s)
        assert entry.attempt_entry(db, cand()) is None
        log = last_log(db)
        assert log.reason == "MAX_POSITIONS:5" and log.decision == "SKIPPED"
        assert available(db) == pytest.approx(LEDGER_AVAILABLE) and not lock_held(db, "ABC")

    def test_one_below_max_positions_allows(self, env):
        db, _, _ = env
        for _ in range(4):
            mkpos(db, status="OPEN")
        assert entry.attempt_entry(db, cand()) is not None

    def test_symbol_held_by_the_other_service(self, env):
        db, b, _ = env
        db.add(models.SharedSymbolLock(symbol="ABC", held_by_service="real-trade-service", held_by_mode="REAL"))
        db.commit()
        assert entry.attempt_entry(db, cand()) is None
        log = last_log(db)
        assert log.reason == "SYMBOL_HELD_BY_OTHER_SERVICE"
        assert available(db) == pytest.approx(LEDGER_AVAILABLE) and n_positions(db) == 0
        assert db.query(models.SharedSymbolLock).one().held_by_service == "real-trade-service"  # untouched

    def test_penny_stock_rejected_and_lock_released(self, env):
        db, _, _ = env
        assert entry.attempt_entry(db, cand(ltp=19.99)) is None
        assert_clean_skip(env, "ABC", "PENNY_STOCK:")

    def test_price_exactly_at_floor_is_allowed(self, env):
        db, b, _ = env
        b.stop_pct = 2.0
        assert entry.attempt_entry(db, cand(ltp=20.0)) is not None

    def test_range_gate_rejects_entry_at_the_high(self, env):
        db, b, _ = env
        b.ticks["ABC"] = [100.0, 200.0] + [150.0] * 8
        assert entry.attempt_entry(db, cand(ltp=200.0)) is None
        assert_clean_skip(env, "ABC", "RANGE_GATE:NEAR_DAY_HIGH")

    def test_range_gate_allows_entry_lower_in_the_range(self, env):
        db, b, _ = env
        b.ticks["ABC"] = [100.0, 200.0] + [150.0] * 8
        assert entry.attempt_entry(db, cand(ltp=150.0)) is not None

    def test_reentry_guard_rejects_chasing(self, env):
        db, _, _ = env
        mkpos(db, "ABC", status="TARGET_HIT", exit_price=500.0, closed_min_ago=5)
        assert entry.attempt_entry(db, cand(ltp=500.0)) is None
        log = last_log(db)
        assert log.reason.startswith("REENTRY_COOLDOWN:") and log.decision == "SKIPPED"
        assert not lock_held(db, "ABC") and available(db) == pytest.approx(LEDGER_AVAILABLE)
        assert n_positions(db) == 1                                  # only the pre-existing closed one

    def test_reentry_guard_allows_a_real_pullback(self, env):
        db, _, _ = env
        mkpos(db, "ABC", status="TARGET_HIT", exit_price=500.0, closed_min_ago=5)
        assert entry.attempt_entry(db, cand(ltp=490.0)) is not None

    def test_insufficient_capital_raises_and_cleans_up(self, env):
        db, b, _ = env
        led = ledger._get_or_create(db)
        led.available_capital = 10_000.0                             # < ₹20,000 needed
        db.commit()
        with pytest.raises(entry.InsufficientCapitalSkip, match="INSUFFICIENT_CAPITAL"):
            entry.attempt_entry(db, cand())
        assert_clean_skip(env, "ABC", "INSUFFICIENT_CAPITAL", capital=10_000.0)

    def test_empty_pool_raises_insufficient_capital(self, env):
        db, _, _ = env
        led = ledger._get_or_create(db)
        led.total_allocated_capital = 0.0
        db.commit()
        with pytest.raises(entry.InsufficientCapitalSkip):
            entry.attempt_entry(db, cand())

    def test_security_not_found_returns_capital_and_lock(self, env):
        db, b, _ = env
        b.sec_error = dhan_client.SecurityNotResolvedError("no such symbol")
        assert entry.attempt_entry(db, cand()) is None
        assert_clean_skip(env, "ABC", "SECURITY_NOT_FOUND:")

    def test_shared_order_budget_exhausted_returns_capital_and_lock(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "SHARED_DAILY_ORDER_BUDGET", 0)
        assert entry.attempt_entry(db, cand()) is None
        assert_clean_skip(env, "ABC", "SHARED_ORDER_BUDGET_EXHAUSTED")
        assert gate_of(db).orders_placed_today == 0


# ── attempt_entry: sizing ────────────────────────────────────────────────────
class TestAttemptEntrySizing:
    def test_quantity_floors_position_value_over_price(self, env):
        db, _, _ = env
        pos = entry.attempt_entry(db, cand(ltp=333.0))               # 20000/333 = 60.06 -> 60
        assert pos.quantity == 60 and pos.capital_risked == pytest.approx(POSITION_VALUE)

    def test_one_share_floor_tops_up_the_reservation(self, env):
        db, b, _ = env
        pos = entry.attempt_entry(db, cand(ltp=30_000.0))            # 1 share costs more than 20,000
        assert pos.quantity == 1
        assert pos.capital_risked == pytest.approx(30_000.0)         # ledger now backs the real cost
        assert available(db) == pytest.approx(LEDGER_AVAILABLE - 30_000.0)
        assert b.of("place_super_order")[0]["quantity"] == 1

    def test_unaffordable_one_share_floor_is_a_capital_skip_and_cleans_up(self, env):
        db, b, _ = env
        led = ledger._get_or_create(db)
        led.available_capital = 22_000.0                             # 20k reserved, 2k left < 10k shortfall
        db.commit()
        with pytest.raises(entry.InsufficientCapitalSkip, match="MIN_QTY"):
            entry.attempt_entry(db, cand(ltp=30_000.0))
        assert_clean_skip(env, "ABC", "INSUFFICIENT_CAPITAL_FOR_MIN_QTY:shortfall=10000.00", capital=22_000.0)

    def test_first_live_order_is_forced_to_one_share(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE", True)
        g = gate_of(db)
        g.first_live_order_done = False
        db.commit()
        pos = entry.attempt_entry(db, cand(ltp=500.0))
        assert pos.quantity == 1 and pos.is_first_live_order is True
        assert b.of("place_super_order")[0]["quantity"] == 1
        assert gate_of(db).first_live_order_done is True
        assert pos.capital_risked == pytest.approx(POSITION_VALUE)   # reservation is not shrunk

    def test_first_live_order_without_override_keeps_normal_size_but_sets_flag(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.first_live_order_done = False
        db.commit()
        pos = entry.attempt_entry(db, cand(ltp=500.0))
        assert pos.quantity == 40 and pos.is_first_live_order is True
        assert gate_of(db).first_live_order_done is True

    def test_override_never_applies_after_the_first_order(self, env, monkeypatch):
        db, _, _ = env
        monkeypatch.setattr(config, "FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE", True)
        assert entry.attempt_entry(db, cand(ltp=500.0)).quantity == 40

    def test_failed_first_order_does_not_consume_the_first_live_flag(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE", True)
        g = gate_of(db)
        g.first_live_order_done = False
        db.commit()
        b.super_script = [RuntimeError(GENERIC_MSG)]
        assert entry.attempt_entry(db, cand()) is None
        assert gate_of(db).first_live_order_done is False


# ── attempt_entry: broker rejects the BUY ────────────────────────────────────
ORDER_FAILURES = [
    (RESTRICTED_MSG, "ORDER_FAILED_INTRADAY_RESTRICTED:", True),
    (CUTOFF_MSG,     "ORDER_FAILED_INTRADAY_CUTOFF:",     False),
    (FUNDS_MSG,      "ORDER_FAILED_INSUFFICIENT_FUNDS:",  False),
    (CIRCUIT_MSG,    "ORDER_FAILED_CIRCUIT_LIMIT:",       True),
    (GENERIC_MSG,    "ORDER_FAILED:",                     False),
]


class TestAttemptEntryOrderFailures:
    @pytest.mark.parametrize("msg,prefix,records_restriction", ORDER_FAILURES)
    def test_rejection_leaves_nothing_behind(self, env, msg, prefix, records_restriction):
        db, b, sent = env
        b.super_script = [RuntimeError(msg)]
        assert entry.attempt_entry(db, cand("t2tco")) is None
        log = last_log(db)
        assert log.decision == "SKIPPED"
        assert log.reason.startswith(prefix)
        assert msg in log.reason
        assert n_positions(db) == 0
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)      # full reservation returned
        assert not lock_held(db, "t2tco")
        g = gate_of(db)
        assert g.orders_placed_today == 0 and g.first_live_order_done is True
        assert sent["info"] == []                                     # no "BUY placed" message
        n_restricted = db.query(models.ScalpIntradayRestrictedSecurity).count()
        assert n_restricted == (1 if records_restriction else 0)
        if records_restriction:
            assert db.query(models.ScalpIntradayRestrictedSecurity).one().symbol == "T2TCO"

    @pytest.mark.parametrize("msg", [RESTRICTED_MSG, CIRCUIT_MSG])
    def test_restriction_recording_failure_is_swallowed(self, env, monkeypatch, msg):
        db, b, _ = env
        b.super_script = [RuntimeError(msg)]

        def boom(*a, **k):
            raise RuntimeError("db hiccup")
        monkeypatch.setattr(entry.intraday_eligibility, "record_restriction", boom)
        assert entry.attempt_entry(db, cand()) is None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE) and not lock_held(db, "ABC")

    def test_plain_order_failure_is_handled_identically(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", False)
        b.plain_script = [RuntimeError(FUNDS_MSG)]
        assert entry.attempt_entry(db, cand()) is None
        assert last_log(db).reason.startswith("ORDER_FAILED_INSUFFICIENT_FUNDS:")
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_failure_after_top_up_returns_the_topped_up_amount(self, env):
        db, b, _ = env
        b.super_script = [RuntimeError(GENERIC_MSG)]
        assert entry.attempt_entry(db, cand(ltp=30_000.0)) is None    # reserved 30k via top-up
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)


# ── attempt_manual_entry ─────────────────────────────────────────────────────
class TestManualEntry:
    def test_not_armed(self, env):
        db, b, _ = env
        g = gate_of(db)
        g.is_armed = False
        db.commit()
        with pytest.raises(entry.ManualEntryRejected, match="not armed"):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert b.calls == []

    def test_kill_switch(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.daily_loss_kill_switch_tripped, g.daily_loss_kill_switch_tripped_date = True, TODAY
        db.commit()
        with pytest.raises(entry.ManualEntryRejected, match="kill switch"):
            entry.attempt_manual_entry(db, "ABC", 500.0)

    def test_order_budget_exhausted(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.orders_placed_today, g.orders_placed_today_date = 300, TODAY
        db.commit()
        with pytest.raises(entry.ManualEntryRejected, match="order budget exhausted"):
            entry.attempt_manual_entry(db, "ABC", 500.0)

    def test_max_positions(self, env):
        db, _, _ = env
        for _ in range(5):
            mkpos(db, status="OPEN")
        with pytest.raises(entry.ManualEntryRejected, match=r"Max concurrent positions reached \(5/5\)"):
            entry.attempt_manual_entry(db, "ABC", 500.0)

    @pytest.mark.parametrize("ltp", [0.0, -3.0])
    def test_no_valid_price(self, env, ltp):
        db, _, _ = env
        with pytest.raises(entry.ManualEntryRejected, match="No valid live price"):
            entry.attempt_manual_entry(db, "ABC", ltp)

    @pytest.mark.parametrize("status", ["OPEN", "EXIT_LEGS_REJECTED"])
    def test_existing_position_in_same_symbol_is_rejected(self, env, status):
        db, b, _ = env
        p = mkpos(db, "ABC", status=status)
        with pytest.raises(entry.ManualEntryRejected, match=rf"already has an open position here \(id={p.id}"):
            entry.attempt_manual_entry(db, " abc ", 500.0)            # symbol is normalised first
        assert b.of("place_super_order") == []

    def test_closed_position_in_same_symbol_does_not_block(self, env):
        db, _, _ = env
        mkpos(db, "ABC", status="TARGET_HIT", closed_min_ago=1, exit_price=500.0)
        assert entry.attempt_manual_entry(db, "ABC", 500.0).status == "OPEN"   # no re-entry cooldown on manual

    def test_symbol_held_by_other_service(self, env):
        db, b, _ = env
        db.add(models.SharedSymbolLock(symbol="ABC", held_by_service="real-trade-service", held_by_mode="REAL"))
        db.commit()
        with pytest.raises(entry.ManualEntryRejected, match="held by real-trade-service"):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_success_auto_sized(self, env):
        db, b, sent = env
        pos = entry.attempt_manual_entry(db, "  abc ", 500.0)
        assert (pos.symbol, pos.window_source, pos.status) == ("ABC", "MANUAL", "OPEN")
        assert pos.quantity == 40 and pos.capital_risked == pytest.approx(POSITION_VALUE)
        assert pos.dhan_super_order_id == pos.dhan_entry_order_id == "SO1"
        assert pos.breakeven_trigger_pct == 0.5
        kw = b.of("place_super_order")[0]
        assert kw["tag"] == "MANUAL" and kw["quantity"] == 40 and kw["is_armed"] is True
        assert b.of("compute_levels")[0]["pct_change"] == 0.0         # manual pick: no scan signal
        assert available(db) == pytest.approx(LEDGER_AVAILABLE - POSITION_VALUE)
        assert lock_held(db, "ABC")
        assert gate_of(db).orders_placed_today == 1
        assert db.query(models.SharedOrderBudget).one().orders_placed_today == 1
        log = last_log(db)
        assert (log.window_source, log.decision, log.reason) == ("MANUAL", "ENTERED", "MANUAL_BUY:SUPER_ORDER=SO1")
        assert any("Manual BUY placed" in m and "ABC" in m for m in sent["info"])

    def test_success_with_exact_quantity(self, env):
        db, b, _ = env
        pos = entry.attempt_manual_entry(db, "ABC", 500.0, quantity=7)
        assert pos.quantity == 7 and pos.capital_risked == pytest.approx(3_500.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE - 3_500.0)
        assert b.of("place_super_order")[0]["quantity"] == 7

    def test_exact_quantity_can_exceed_the_auto_size(self, env):
        db, _, _ = env
        pos = entry.attempt_manual_entry(db, "ABC", 500.0, quantity=80)   # ₹40k > the ₹20k auto size
        assert pos.quantity == 80

    def test_exact_quantity_beyond_available_capital_is_rejected_cleanly(self, env):
        db, b, _ = env
        with pytest.raises(entry.ManualEntryRejected, match="Insufficient capital: need ₹60,000.00 for 120 shares"):
            entry.attempt_manual_entry(db, "ABC", 500.0, quantity=120)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE) and not lock_held(db, "ABC")
        assert b.of("place_super_order") == []

    def test_auto_size_insufficient_capital(self, env):
        db, _, _ = env
        led = ledger._get_or_create(db)
        led.available_capital = 5_000.0
        db.commit()
        with pytest.raises(entry.ManualEntryRejected, match="Insufficient available capital"):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert available(db) == pytest.approx(5_000.0) and not lock_held(db, "ABC")

    def test_auto_size_one_share_floor_tops_up(self, env):
        db, _, _ = env
        pos = entry.attempt_manual_entry(db, "ABC", 30_000.0)
        assert pos.quantity == 1 and pos.capital_risked == pytest.approx(30_000.0)

    def test_auto_size_unaffordable_one_share_floor(self, env):
        db, _, _ = env
        led = ledger._get_or_create(db)
        led.available_capital = 22_000.0
        db.commit()
        with pytest.raises(entry.ManualEntryRejected, match="minimum 1-share order"):
            entry.attempt_manual_entry(db, "ABC", 30_000.0)
        assert available(db) == pytest.approx(22_000.0) and not lock_held(db, "ABC")

    def test_security_not_found(self, env):
        db, b, _ = env
        b.sec_error = dhan_client.SecurityNotResolvedError("nope")
        with pytest.raises(entry.ManualEntryRejected, match="Could not resolve a Dhan security id"):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE) and not lock_held(db, "ABC")

    def test_shared_budget_exhausted(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "SHARED_DAILY_ORDER_BUDGET", 0)
        with pytest.raises(entry.ManualEntryRejected, match="order-rate budget exhausted"):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE) and not lock_held(db, "ABC")
        assert b.of("place_super_order") == []

    def test_broker_rejection_cleans_up_and_logs(self, env):
        db, b, sent = env
        b.super_script = [RuntimeError(CIRCUIT_MSG)]
        with pytest.raises(entry.ManualEntryRejected, match="Dhan rejected the manual BUY"):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE) and not lock_held(db, "ABC")
        assert n_positions(db) == 0
        log = last_log(db)
        assert log.decision == "SKIPPED" and log.reason.startswith("MANUAL_ORDER_FAILED:")
        assert gate_of(db).orders_placed_today == 0 and sent["info"] == []

    def test_plain_order_fallback(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", False)
        pos = entry.attempt_manual_entry(db, "ABC", 500.0)
        kw = b.of("place_order")[0]
        assert kw["tag"] == "MANUAL" and kw["price"] == 0.0
        assert pos.dhan_super_order_id is None
        assert last_log(db).reason == "MANUAL_BUY:SUPER_ORDER=plain_order"

    def test_first_live_order_flag_is_set_but_quantity_is_not_forced(self, env, monkeypatch):
        db, _, _ = env
        monkeypatch.setattr(config, "FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE", True)
        g = gate_of(db)
        g.first_live_order_done = False
        db.commit()
        pos = entry.attempt_manual_entry(db, "ABC", 500.0)
        assert pos.is_first_live_order is True and pos.quantity == 40
        assert gate_of(db).first_live_order_done is True

    def test_stale_daily_counter_resets(self, env):
        db, _, _ = env
        g = gate_of(db)
        g.orders_placed_today, g.orders_placed_today_date = 300, "2026-09-18"
        db.commit()
        entry.attempt_manual_entry(db, "ABC", 500.0)
        g = gate_of(db)
        assert g.orders_placed_today == 1 and g.orders_placed_today_date == TODAY


# ── Regressions for the bugs found (and fixed) by the 2026-09-21 audit ───────
class TestAuditRegressions:
    """Each of these failed against the pre-audit code (see AUDIT_REPORT.md). They now
    guard the fixes: a restricted BUY must be remembered, and a failed entry must never
    leave capital reserved or a cross-service symbol lock held."""

    def test_restricted_buy_rejection_should_record_the_restriction(self, env):
        db, b, _ = env
        b.super_script = [RuntimeError(RESTRICTED_MSG)]
        assert entry.attempt_entry(db, cand("t2tco")) is None
        assert db.query(models.ScalpIntradayRestrictedSecurity).one().symbol == "T2TCO"

    def test_auto_entry_generic_security_lookup_error_should_not_leak_capital_or_lock(self, env):
        db, b, _ = env
        b.sec_error = dhan_client.DhanNotConnectedError("No Dhan credentials stored.")
        with pytest.raises(dhan_client.DhanNotConnectedError):
            entry.attempt_entry(db, cand())
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)
        assert not lock_held(db, "ABC")

    def test_manual_entry_generic_security_lookup_error_should_not_leak_capital_or_lock(self, env):
        db, b, _ = env
        b.sec_error = dhan_client.DhanNotConnectedError("No Dhan credentials stored.")
        with pytest.raises(dhan_client.DhanNotConnectedError):
            entry.attempt_manual_entry(db, "ABC", 500.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)
        assert not lock_held(db, "ABC")

    @pytest.mark.parametrize("qty", [0, -5])
    def test_manual_entry_non_positive_quantity_should_release_the_symbol_lock(self, env, qty):
        db, _, _ = env
        with pytest.raises(entry.ManualEntryRejected, match="positive integer"):
            entry.attempt_manual_entry(db, "ABC", 500.0, quantity=qty)
        assert not lock_held(db, "ABC")

    def test_plain_order_fallback_should_record_the_buy_order_id(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", False)
        pos = entry.attempt_entry(db, cand())
        assert pos.dhan_entry_order_id == "PO1"
