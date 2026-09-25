"""
tests/test_reconcile.py — offline tests for orders/reconcile.py (position-stocks-service).

reconcile.py is where a position stops being "OPEN": it reads Dhan's order
book / super-order book / trade history and books the REAL exit price, realized
P&L, capital release and symbol-lock release. A mistake here corrupts the
trade ledger (phantom losses, double-released capital, a tripped kill switch)
without anything ever raising an error, so every branch is pinned.

Offline: real models on in-memory SQLite (real ledger / symbol lock / overnight
stop accounting), scripted fake Dhan responses, patched notifier and IST date.

Sections:
  TestExtractLegPrice           fill-price key fallbacks (the "never a phantom 0.0" fix)
  TestListPending               GET /reconcile/pending diagnostic
  TestBackfillLegacy            pre-session40 rows: match a SELL by security/qty
  TestReconcileEodPending       resolve a flat-SELL's real fill / dead SELL
  TestResolvePendingWithPrice   P&L math incl. overnight partials, late booking
  TestResolveStuckPending       prior-day rows via trade history, age-out, throttle
  TestOvernightStops            protective-stop driver
  TestRunExitReconciliation     the super-order pass: hits, entry-fill correction, dead legs
  TestRetentionCleanup          trade-history retention
  TestAuditRegressions          a double capital release found here — now fixed

Run from services/position-stocks-service:
    python3 -m pytest tests/test_reconcile.py -q --cov=orders --cov-report=term-missing
"""
from __future__ import annotations

import itertools
import os
import sys
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
from orders import reconcile

TODAY = "2026-09-21"
LEDGER_TOTAL = 100_000.0
LEDGER_AVAILABLE = 50_000.0


class Broker:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.plain_orders: list[dict] = []
        self.super_orders: list[dict] = []
        self.trades: list[dict] = []
        self.plain_error: BaseException | None = None
        self.super_error: BaseException | None = None
        self.trades_error: BaseException | None = None
        self.modify_error: BaseException | None = None

    def of(self, name):
        return [c[1] for c in self.calls if c[0] == name]


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    b = Broker()
    sent = {"info": [], "critical": []}
    monkeypatch.setattr(notifier, "notify_sync", lambda m, *a, **k: sent["info"].append(m) or True)
    monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda m, *a, **k: sent["info"].append(m))
    monkeypatch.setattr(notifier, "notify_critical", lambda m, *a, **k: sent["critical"].append(m))

    def _plain(db_):
        b.calls.append(("get_order_list", {}))
        if b.plain_error:
            raise b.plain_error
        return list(b.plain_orders)

    def _super(db_):
        b.calls.append(("get_super_order_list", {}))
        if b.super_error:
            raise b.super_error
        return list(b.super_orders)

    def _trades(db_, f, t):
        b.calls.append(("get_trade_history", {"from": f, "to": t}))
        if b.trades_error:
            raise b.trades_error
        return list(b.trades)

    def _modify(db_, **kw):
        b.calls.append(("modify_super_order", kw))
        if b.modify_error:
            raise b.modify_error
        return {}

    monkeypatch.setattr(dhan_client, "get_order_list", _plain)
    monkeypatch.setattr(dhan_client, "get_super_order_list", _super)
    monkeypatch.setattr(dhan_client, "get_trade_history", _trades)
    monkeypatch.setattr(dhan_client, "modify_super_order", _modify)
    monkeypatch.setattr(reconcile, "ist_today_str", lambda: TODAY)
    monkeypatch.setattr(reconcile, "_last_stuck_sweep_ts", 0.0)
    monkeypatch.setattr(reconcile, "_stuck_alerted", set())
    monkeypatch.setattr(reconcile, "_backfill_unresolved_notified", set())
    for k, v in dict(PENDING_RECONCILE_SWEEP_INTERVAL_S=600.0, PENDING_RECONCILE_MAX_AGE_DAYS=3,
                     TRADE_HISTORY_RETENTION_DAYS=3.0, USE_SUPER_ORDER=True,
                     MAX_DAILY_LOSS_PCT_OF_POOL=4.0).items():
        monkeypatch.setattr(config, k, v)

    led = ledger._get_or_create(db)
    led.total_allocated_capital, led.available_capital = LEDGER_TOTAL, LEDGER_AVAILABLE
    db.commit()
    yield db, b, sent
    db.close()


# ── Builders / readers ───────────────────────────────────────────────────────
_ids = itertools.count(1)


def days_ago(n, hour_utc=5):
    d = datetime(2026, 9, 21, hour_utc, 0, tzinfo=timezone.utc) - timedelta(days=n)
    return d


def mkpos(db, symbol=None, *, status="OPEN", entry=100.0, qty=10, capital=None, super_id=None,
          exit_order_id=None, error_message=None, closed_at=None, exit_price=None, realized_pnl=None,
          claim_lock=True, **extra):
    n = next(_ids)
    symbol = symbol or f"SYM{n}"
    p = models.ScalpPosition(
        symbol=symbol, dhan_security_id=str(1000 + n), window_source="5m", status=status,
        entry_price=entry, quantity=qty, target_price=round(entry * 1.02, 2), stop_price=round(entry * 0.99, 2),
        adaptive_target_pct=2.0, adaptive_stop_pct=1.0,
        capital_risked=entry * qty if capital is None else capital,
        dhan_super_order_id=super_id, dhan_exit_order_id=exit_order_id, error_message=error_message,
        closed_at=closed_at, exit_price=exit_price, realized_pnl=realized_pnl,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=2), **extra,
    )
    db.add(p)
    db.commit()
    if claim_lock:
        shared_symbol_lock.try_claim(db, symbol)
    return p


def pending(db, status="EOD_SQUAREOFF", **kw):
    """A flat-SELL placeholder row exactly as eod_squareoff.py leaves it."""
    kw.setdefault("exit_price", kw.get("entry", 100.0))
    kw.setdefault("realized_pnl", 0.0)
    kw.setdefault("closed_at", days_ago(0))
    kw.setdefault("error_message", f"{status}_PENDING_RECONCILE: exit price unknown")
    kw.setdefault("claim_lock", False)
    return mkpos(db, status=status, **kw)


def plain_row(oid, status="TRADED", avg=None, otype="MARKET", **kw):
    r = {"orderId": oid, "orderStatus": status, "orderType": otype}
    if avg is not None:
        r["averageTradedPrice"] = avg
    r.update(kw)
    return r


def super_row(oid, status="TRADED", avg=None, target=None, stop=None, **kw):
    row = {"orderId": oid, "orderStatus": status, "legDetails": []}
    if avg is not None:
        row["averageTradedPrice"] = avg
    for name, spec in (("TARGET_LEG", target), ("STOP_LOSS_LEG", stop)):
        if spec:
            leg = {"legName": name, "orderId": oid, "orderStatus": spec[0]}
            if len(spec) > 1 and spec[1] is not None:
                leg["price"] = spec[1]
            if len(spec) > 2 and spec[2] is not None:
                leg["averageTradedPrice"] = spec[2]
            row["legDetails"].append(leg)
    row.update(kw)
    return row


def trade(oid, secid, qty, price, day="2026-09-20", side="SELL"):
    return {"orderId": oid, "transactionType": side, "securityId": str(secid),
            "tradedQuantity": qty, "tradedPrice": price, "createTime": f"{day} 10:10:00"}


def state(db):
    return ledger.get_state(db)


def available(db):
    return state(db)["available_capital"]


def lock_held(db, symbol):
    return db.query(models.SharedSymbolLock).filter_by(symbol=symbol.upper()).first() is not None


# ── _extract_leg_price ───────────────────────────────────────────────────────
class TestExtractLegPrice:
    @pytest.mark.parametrize("key", ["averageTradedPrice", "tradedPrice", "avgPrice", "avgTradedPrice"])
    def test_every_known_fill_price_key_is_used(self, key):
        assert reconcile._extract_leg_price({key: "101.5"}, {}, 90.0) == 101.5

    def test_earlier_key_wins(self):
        leg = {"avgTradedPrice": 3.0, "averageTradedPrice": 1.0, "tradedPrice": 2.0}
        assert reconcile._extract_leg_price(leg, {}) == 1.0

    def test_unparseable_fill_key_falls_through_to_the_next_key(self):
        assert reconcile._extract_leg_price({"averageTradedPrice": "n/a", "tradedPrice": 7}, {}) == 7.0

    def test_zero_fill_price_is_treated_as_missing(self):
        assert reconcile._extract_leg_price({"averageTradedPrice": 0, "price": 55.0}, {}) == 55.0

    def test_leg_static_price_is_the_second_choice(self):
        assert reconcile._extract_leg_price({"price": "55.25"}, {"averageTradedPrice": 1.0}, 9.0) == 55.25

    def test_bad_leg_price_falls_to_parent_average(self):
        assert reconcile._extract_leg_price({"price": "x"}, {"averageTradedPrice": "60"}, 9.0) == 60.0

    def test_parent_average_is_the_third_choice(self):
        assert reconcile._extract_leg_price({}, {"averageTradedPrice": 61.5}, 9.0) == 61.5

    def test_bad_parent_average_falls_to_own_trigger_price(self):
        assert reconcile._extract_leg_price({}, {"averageTradedPrice": "zzz"}, 9.5) == 9.5

    def test_own_trigger_price_beats_a_phantom_zero(self):
        # the historic bug: with nothing usable this used to return 0.0 => a fake 100% loss
        assert reconcile._extract_leg_price({}, {}, 102.0) == 102.0

    def test_nothing_at_all_returns_zero_only_when_no_fallback_given(self):
        assert reconcile._extract_leg_price({}, {}) == 0.0


def test_ist_date_rolls_over_at_1830_utc():
    assert reconcile._ist_date_str(datetime(2026, 9, 20, 18, 29, tzinfo=timezone.utc)) == "2026-09-20"
    assert reconcile._ist_date_str(datetime(2026, 9, 20, 18, 30, tzinfo=timezone.utc)) == "2026-09-21"
    assert reconcile._ist_date_str(datetime(2026, 9, 20, 5, 0)) == "2026-09-20"       # naive == UTC


# ── list_pending_reconcile ───────────────────────────────────────────────────
class TestListPending:
    def test_only_sentinel_rows_newest_first_with_age_and_route(self, env):
        db, _, _ = env
        old = pending(db, closed_at=days_ago(2), exit_order_id="X1")
        new = pending(db, "MANUAL_EXIT")
        mkpos(db, status="TARGET_HIT", error_message=None)                    # not pending
        mkpos(db, status="ERROR", error_message="some other failure")          # not pending
        rows = reconcile.list_pending_reconcile(db)
        assert [r["id"] for r in rows] == [new.id, old.id]
        assert rows[0]["age_days"] == 0 and rows[0]["resolvable_via"] == "order_list (same day)"
        assert rows[1]["age_days"] == 2 and rows[1]["resolvable_via"] == "trade_history (prior day)"
        assert rows[1]["dhan_exit_order_id"] == "X1" and rows[1]["closed_day_ist"] == "2026-09-19"

    def test_row_without_closed_at_has_unknown_age(self, env):
        db, _, _ = env
        pending(db, closed_at=None)
        db.query(models.ScalpPosition).update({"closed_at": None})
        db.commit()
        r = reconcile.list_pending_reconcile(db)[0]
        assert r["closed_day_ist"] is None and r["age_days"] is None
        assert r["resolvable_via"] == "trade_history (prior day)"

    def test_status_is_not_a_filter(self, env):
        db, _, _ = env
        pending(db, "EOD_SQUAREOFF")
        mkpos(db, status="TARGET_HIT", error_message="STOP_HIT_PENDING_RECONCILE: drifted status",
              closed_at=days_ago(1))
        assert len(reconcile.list_pending_reconcile(db)) == 2


# ── _backfill_legacy_eod_exit_order_ids ──────────────────────────────────────
def sell(oid, secid, qty, status="TRADED", **kw):
    r = {"orderId": oid, "transactionType": "SELL", "securityId": str(secid), "quantity": qty, "orderStatus": status}
    r.update(kw)
    return r


class TestBackfillLegacy:
    def test_nothing_to_do_makes_no_broker_call(self, env):
        db, b, _ = env
        p = pending(db, exit_order_id="HAS1")
        assert reconcile._backfill_legacy_eod_exit_order_ids(db, [p]) == 0
        assert b.calls == []

    def test_order_list_failure_is_swallowed(self, env):
        db, b, sent = env
        b.plain_error = RuntimeError("dhan down")
        assert reconcile._backfill_legacy_eod_exit_order_ids(db, [pending(db)]) == 0
        assert sent["critical"] == []

    def test_matching_sell_is_adopted_and_committed(self, env):
        db, b, _ = env
        p = pending(db, qty=10)
        b.plain_orders = [sell("S1", p.dhan_security_id, 10)]
        assert reconcile._backfill_legacy_eod_exit_order_ids(db, [p]) == 1
        db.expire_all()
        assert p.dhan_exit_order_id == "S1"

    def test_filled_candidate_preferred_over_open_one(self, env):
        db, b, _ = env
        p = pending(db, qty=10)
        b.plain_orders = [sell("PEND", p.dhan_security_id, 10, status="PENDING"),
                          sell("DONE", p.dhan_security_id, 10, status="TRADED")]
        reconcile._backfill_legacy_eod_exit_order_ids(db, [p])
        assert p.dhan_exit_order_id == "DONE"

    @pytest.mark.parametrize("mut", [
        {"transactionType": "BUY"}, {"quantity": 9}, {"securityId": "999999"}])
    def test_non_matching_orders_are_ignored(self, env, mut):
        db, b, sent = env
        p = pending(db, qty=10)
        row = sell("S1", p.dhan_security_id, 10)
        row.update(mut)
        b.plain_orders = [row]
        assert reconcile._backfill_legacy_eod_exit_order_ids(db, [p]) == 0
        assert p.dhan_exit_order_id is None

    def test_snake_case_keys_are_understood(self, env):
        db, b, _ = env
        p = pending(db, qty=10)
        b.plain_orders = [{"order_id": "S9", "transaction_type": "sell", "security_id": p.dhan_security_id,
                           "quantity": 10, "order_status": "TRADED"}]
        assert reconcile._backfill_legacy_eod_exit_order_ids(db, [p]) == 1
        assert p.dhan_exit_order_id == "S9"

    def test_match_with_blank_order_id_is_skipped(self, env):
        """A matched row with no usable orderId/order_id (line 232's `if not
        oid: continue`) must not be adopted — nothing to store, nothing to claim."""
        db, b, _ = env
        p = pending(db, qty=10)
        b.plain_orders = [sell("", p.dhan_security_id, 10)]
        assert reconcile._backfill_legacy_eod_exit_order_ids(db, [p]) == 0
        assert p.dhan_exit_order_id is None

    def test_one_order_is_never_claimed_by_two_positions(self, env):
        db, b, _ = env
        a = pending(db, qty=10)
        c = pending(db, qty=10)
        c.dhan_security_id = a.dhan_security_id          # ambiguous twin
        db.commit()
        b.plain_orders = [sell("ONLY", a.dhan_security_id, 10)]
        assert reconcile._backfill_legacy_eod_exit_order_ids(db, [a, c]) == 1
        assert {a.dhan_exit_order_id, c.dhan_exit_order_id} == {"ONLY", None}

    def test_id_already_held_by_another_pending_row_is_not_reused(self, env):
        db, b, _ = env
        holder = pending(db, qty=10, exit_order_id="TAKEN")
        legacy = pending(db, qty=10)
        legacy.dhan_security_id = holder.dhan_security_id
        db.commit()
        b.plain_orders = [sell("TAKEN", holder.dhan_security_id, 10)]
        assert reconcile._backfill_legacy_eod_exit_order_ids(db, [holder, legacy]) == 0

    def test_unresolved_rows_alert_once_per_position(self, env):
        db, b, sent = env
        p = pending(db)
        reconcile._backfill_legacy_eod_exit_order_ids(db, [p])
        reconcile._backfill_legacy_eod_exit_order_ids(db, [p])
        assert len(sent["critical"]) == 1 and p.symbol in sent["critical"][0]

    def test_alert_lists_at_most_ten_symbols(self, env):
        db, b, sent = env
        rows = [pending(db) for _ in range(12)]
        reconcile._backfill_legacy_eod_exit_order_ids(db, rows)
        assert "12 pre-session40" in sent["critical"][0] and "..." in sent["critical"][0]


# ── _reconcile_eod_pending ───────────────────────────────────────────────────
class TestReconcileEodPending:
    def test_no_candidates_no_broker_call(self, env):
        db, b, _ = env
        assert reconcile._reconcile_eod_pending(db, [pending(db)]) == 0     # no exit order id
        assert b.calls == []

    def test_order_list_failure_is_swallowed(self, env):
        db, b, _ = env
        b.plain_error = RuntimeError("dhan down")
        assert reconcile._reconcile_eod_pending(db, [pending(db, exit_order_id="X1")]) == 0

    def test_order_not_visible_yet_is_left_alone(self, env):
        db, b, _ = env
        p = pending(db, exit_order_id="X1")
        b.plain_orders = [plain_row("OTHER", avg=99.0)]
        assert reconcile._reconcile_eod_pending(db, [p]) == 0
        assert p.error_message.endswith("exit price unknown") and p.exit_price == 100.0

    def test_filled_sell_books_real_price_pnl_and_ledger(self, env):
        db, b, sent = env
        p = pending(db, entry=100.0, qty=10, exit_order_id="X1")
        b.plain_orders = [plain_row("X1", avg="103.5")]
        assert reconcile._reconcile_eod_pending(db, [p]) == 1
        assert p.exit_price == 103.5 and p.realized_pnl == pytest.approx(35.0)
        assert p.realized_pnl_pct == pytest.approx(3.5) and p.error_message is None
        assert p.status == "EOD_SQUAREOFF"
        # capital was already returned at SELL time -> only the P&L moves now
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 35.0)
        assert state(db)["realized_pnl_today"] == pytest.approx(35.0)
        assert any("real fill resolved" in m and "EOD_SQUAREOFF" in m for m in sent["info"])

    @pytest.mark.parametrize("status", ["TRADED", "FILLED", "EXECUTED", "COMPLETE", "traded"])
    def test_every_filled_vocabulary_word_resolves(self, env, status):
        db, b, _ = env
        p = pending(db, exit_order_id="X1")
        b.plain_orders = [plain_row("X1", status=status, avg=101.0)]
        assert reconcile._reconcile_eod_pending(db, [p]) == 1

    def test_snake_case_row_is_understood(self, env):
        db, b, _ = env
        p = pending(db, exit_order_id="X1")
        b.plain_orders = [{"order_id": "X1", "order_status": "TRADED", "average_traded_price": 102.0}]
        assert reconcile._reconcile_eod_pending(db, [p]) == 1 and p.exit_price == 102.0

    @pytest.mark.parametrize("avg", [None, 0, "junk"])
    def test_filled_without_a_usable_price_waits_for_the_next_pass(self, env, avg):
        db, b, _ = env
        p = pending(db, exit_order_id="X1")
        b.plain_orders = [plain_row("X1", avg=avg)]
        assert reconcile._reconcile_eod_pending(db, [p]) == 0
        assert p.error_message.startswith("EOD_SQUAREOFF_PENDING_RECONCILE")
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)

    @pytest.mark.parametrize("otype,alerts", [("MARKET", False), ("LIMIT", False), ("", False),
                                              ("STOP_LOSS", True), ("SL-M", True)])
    def test_unexpected_order_type_alerts_but_still_resolves(self, env, otype, alerts):
        db, b, sent = env
        p = pending(db, exit_order_id="X1")
        b.plain_orders = [plain_row("X1", avg=100.5, otype=otype)]
        assert reconcile._reconcile_eod_pending(db, [p]) == 1
        assert bool(sent["critical"]) is alerts
        if alerts:
            assert "ORDER TYPE MISMATCH" in sent["critical"][0]

    @pytest.mark.parametrize("status,prefix", [("EOD_SQUAREOFF", "EOD_SQUAREOFF_SELL_DEAD"),
                                               ("MANUAL_EXIT", "MANUAL_EXIT_SELL_DEAD"),
                                               ("STAGNATION_EXIT", "STAGNATION_EXIT_SELL_DEAD")])
    @pytest.mark.parametrize("dead", ["REJECTED", "CANCELLED"])
    def test_dead_sell_marks_error_reclaims_capital_and_relocks(self, env, status, prefix, dead):
        db, b, sent = env
        p = pending(db, status, entry=100.0, qty=10, exit_order_id="X1")       # capital_risked 1,000
        b.plain_orders = [plain_row("X1", status=dead)]
        assert reconcile._reconcile_eod_pending(db, [p]) == 1
        assert p.status == "ERROR" and p.error_message.startswith(prefix)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE - 1_000.0)      # premature release undone
        assert lock_held(db, p.symbol)                                          # re-claimed: no duplicate buy
        assert len(sent["critical"]) == 1 and "ZERO fill" in sent["critical"][0]
        assert p.exit_price == 100.0 and state(db)["realized_pnl_today"] == 0.0  # no fake P&L booked

    @pytest.mark.parametrize("status", ["PENDING", "TRANSIT", "PART_TRADED", "OPEN"])
    def test_in_flight_sell_is_left_alone(self, env, status):
        db, b, sent = env
        p = pending(db, exit_order_id="X1")
        b.plain_orders = [plain_row("X1", status=status, avg=100.5)]
        assert reconcile._reconcile_eod_pending(db, [p]) == 0
        assert p.status == "EOD_SQUAREOFF" and sent["critical"] == []


# ── _resolve_pending_with_price ──────────────────────────────────────────────
class TestResolvePendingWithPrice:
    def test_plain_loss(self, env):
        db, _, _ = env
        p = pending(db, entry=100.0, qty=10)
        reconcile._resolve_pending_with_price(db, p, 97.0, late=False)
        assert p.realized_pnl == pytest.approx(-30.0) and p.realized_pnl_pct == pytest.approx(-3.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE - 30.0)

    def test_overnight_partials_are_added_not_overwritten(self, env):
        db, _, _ = env
        # 100 shares held: 30 already stopped out at -₹120 (prior), 70 flat-sold now at +₹2
        p = pending(db, entry=100.0, qty=70, realized_pnl=-120.0,
                    overnight_stop_prior_qty=30, overnight_stop_filled_qty_so_far=0)
        reconcile._resolve_pending_with_price(db, p, 102.0, late=False)
        assert p.realized_pnl == pytest.approx(-120.0 + 140.0)
        assert p.realized_pnl_pct == pytest.approx(20.0 / (100.0 * 100) * 100.0)  # % of the FULL 100 shares
        assert state(db)["realized_pnl_today"] == pytest.approx(140.0)             # ledger gets only THIS sell

    def test_without_partials_prior_pnl_is_ignored(self, env):
        db, _, _ = env
        p = pending(db, entry=100.0, qty=10, realized_pnl=999.0)                   # stale placeholder value
        reconcile._resolve_pending_with_price(db, p, 101.0, late=False)
        assert p.realized_pnl == pytest.approx(10.0)

    def test_late_resolution_skips_todays_daily_loss_counter(self, env):
        db, _, sent = env
        p = pending(db, entry=100.0, qty=10, closed_at=days_ago(2))
        reconcile._resolve_pending_with_price(db, p, 90.0, late=True)
        s = state(db)
        assert s["realized_pnl_today"] == 0.0 and s["realized_pnl_total"] == pytest.approx(-100.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE - 100.0)            # only the P&L moves cash
        assert "resolved late" in sent["info"][0]

    def test_a_big_real_loss_trips_the_kill_switch(self, env):
        db, _, _ = env
        p = pending(db, entry=1000.0, qty=10)                                       # ₹10k position
        reconcile._resolve_pending_with_price(db, p, 590.0, late=False)              # -₹4,100 = 4.1% of pool
        assert state(db)["daily_loss_kill_switch_tripped"] is True

    def test_zero_basis_does_not_divide_by_zero(self, env):
        db, _, _ = env
        p = pending(db, entry=0.0, qty=10, capital=100.0)
        reconcile._resolve_pending_with_price(db, p, 5.0, late=False)
        assert p.realized_pnl_pct == 0.0


# ── resolve_stuck_pending ────────────────────────────────────────────────────
class TestResolveStuckPending:
    def stuck(self, db, age=1, **kw):
        return pending(db, closed_at=days_ago(age), **kw)

    def test_throttled_after_the_first_sweep_unless_forced(self, env):
        db, b, _ = env
        reconcile.resolve_stuck_pending(db)
        assert reconcile.resolve_stuck_pending(db)["skipped"] == "throttled"
        assert "skipped" not in reconcile.resolve_stuck_pending(db, force=True)

    def test_nothing_stuck_makes_no_broker_call(self, env):
        db, b, _ = env
        pending(db)                                     # closed TODAY: same-day path owns it
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["examined"] == 0 and b.calls == []

    def test_row_with_no_closed_at_counts_as_stuck(self, env):
        db, b, _ = env
        p = pending(db)
        p.closed_at = None
        db.commit()
        assert reconcile.resolve_stuck_pending(db, force=True)["examined"] == 1

    def test_inconsistent_but_already_priced_row_is_self_healed(self, env):
        db, b, _ = env
        p = self.stuck(db, entry=100.0, exit_price=103.0, realized_pnl=30.0)
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["self_healed"] == 1 and p.error_message is None
        assert p.exit_price == 103.0 and p.realized_pnl == 30.0

    def test_trade_history_failure_keeps_rows_pending_and_never_ages_them_out(self, env):
        db, b, sent = env
        p = self.stuck(db, age=10)
        b.trades_error = RuntimeError("history down")
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["still_pending"] == 1 and s["aged_out"] == 0
        assert p.error_message.startswith("EOD_SQUAREOFF_PENDING_RECONCILE") and sent["critical"] == []

    def test_resolved_by_order_id_from_trade_history(self, env):
        db, b, _ = env
        p = self.stuck(db, qty=10, exit_order_id="OLD1", entry=100.0)
        b.trades = [trade("OLD1", p.dhan_security_id, 6, 104.0), trade("OLD1", p.dhan_security_id, 4, 105.0)]
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["resolved"] == 1
        assert p.exit_price == pytest.approx(104.4) and p.realized_pnl == pytest.approx(44.0)
        assert p.error_message is None
        assert state(db)["realized_pnl_today"] == 0.0            # late: never hits today's counter
        assert state(db)["realized_pnl_total"] == pytest.approx(44.0)

    def test_partial_traded_quantity_is_never_guessed(self, env):
        db, b, _ = env
        p = self.stuck(db, qty=10, exit_order_id="OLD1")
        b.trades = [trade("OLD1", p.dhan_security_id, 6, 104.0)]
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["resolved"] == 0 and s["still_pending"] == 1
        assert p.exit_price == 100.0

    def test_adopts_the_single_matching_sell_when_no_order_id(self, env):
        db, b, _ = env
        p = self.stuck(db, qty=10, entry=100.0)            # closed 2026-09-20
        b.trades = [trade("FOUND", p.dhan_security_id, 10, 102.0, day="2026-09-20")]
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["resolved"] == 1 and p.dhan_exit_order_id == "FOUND"

    def test_two_matching_sells_are_ambiguous_and_not_adopted(self, env):
        db, b, _ = env
        p = self.stuck(db, qty=10)
        b.trades = [trade("A", p.dhan_security_id, 10, 102.0), trade("B", p.dhan_security_id, 10, 103.0)]
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["resolved"] == 0 and p.dhan_exit_order_id is None

    @pytest.mark.parametrize("kw", [{"day": "2026-09-19"}, {"side": "BUY"}, {"secid": "999"}])
    def test_wrong_day_side_or_security_is_not_adopted(self, env, kw):
        db, b, _ = env
        p = self.stuck(db, qty=10)
        secid = kw.get("secid", p.dhan_security_id)
        b.trades = [trade("X", secid, 10, 102.0, day=kw.get("day", "2026-09-20"), side=kw.get("side", "SELL"))]
        assert reconcile.resolve_stuck_pending(db, force=True)["resolved"] == 0

    def test_ages_out_at_the_max_age_and_alerts_once(self, env):
        db, b, sent = env
        p = self.stuck(db, age=3, exit_order_id="GONE")
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["aged_out"] == 1
        assert p.error_message.startswith("EOD_SQUAREOFF_UNRESOLVED") and "GONE" in p.error_message
        assert len(sent["critical"]) == 1 and "UNRESOLVED" in sent["critical"][0]
        # rewritten sentinel no longer matches %_PENDING_RECONCILE% -> never swept again, never re-alerted
        reconcile.resolve_stuck_pending(db, force=True)
        assert len(sent["critical"]) == 1

    def test_not_aged_out_before_the_max_age(self, env):
        db, b, sent = env
        p = self.stuck(db, age=2)
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["still_pending"] == 1 and s["aged_out"] == 0 and sent["critical"] == []

    def test_status_drift_does_not_hide_a_row_from_the_sweep(self, env):
        # session75 live bug: status overwritten to TARGET_HIT but the sentinel stayed
        db, b, _ = env
        p = mkpos(db, status="TARGET_HIT", closed_at=days_ago(1), exit_order_id="D1", claim_lock=False,
                  error_message="EOD_SQUAREOFF_PENDING_RECONCILE: stale")
        b.trades = [trade("D1", p.dhan_security_id, 10, 101.0)]
        assert reconcile.resolve_stuck_pending(db, force=True)["resolved"] == 1

    def test_one_bad_row_does_not_stop_the_sweep(self, env, monkeypatch):
        db, b, _ = env
        bad = self.stuck(db, exit_order_id="B1")
        good = self.stuck(db, exit_order_id="G1")
        b.trades = [trade("G1", good.dhan_security_id, 10, 101.0)]
        real = reconcile.overnight_stop.aggregate_trades

        def flaky(trades, oid):
            if oid == "B1":
                raise ValueError("bad row")
            return real(trades, oid)
        monkeypatch.setattr(reconcile.overnight_stop, "aggregate_trades", flaky)
        s = reconcile.resolve_stuck_pending(db, force=True)
        assert s["resolved"] == 1 and s["examined"] == 2
        assert good.error_message is None and bad.error_message.endswith("exit price unknown")

    def test_trade_history_window_starts_a_day_before_the_oldest_row(self, env):
        db, b, _ = env
        self.stuck(db, age=2)
        reconcile.resolve_stuck_pending(db, force=True)
        assert b.of("get_trade_history")[0] == {"from": "2026-09-18", "to": TODAY}


# ── _reconcile_overnight_stops ───────────────────────────────────────────────
def stop_row(oid, status, filled, avg, qty=10):
    return {"orderId": oid, "orderStatus": status, "filledQty": filled,
            "averageTradedPrice": avg, "quantity": qty}


class TestOvernightStops:
    def carried(self, db, **kw):
        kw.setdefault("overnight_stop_order_id", "S1")
        return mkpos(db, overnight_converted_to_cnc=True, **kw)

    def test_no_carried_positions_no_broker_call(self, env):
        db, b, _ = env
        mkpos(db)
        assert reconcile._reconcile_overnight_stops(db) == 0 and b.calls == []

    def test_carried_position_without_a_stop_id_is_ignored(self, env):
        db, b, _ = env
        self.carried(db, overnight_stop_order_id=None)
        assert reconcile._reconcile_overnight_stops(db) == 0 and b.calls == []

    def test_order_list_failure_is_swallowed(self, env):
        db, b, _ = env
        self.carried(db)
        b.plain_error = RuntimeError("down")
        assert reconcile._reconcile_overnight_stops(db) == 0

    def test_stop_not_in_todays_book_is_left_for_the_morning_recheck(self, env):
        db, b, _ = env
        p = self.carried(db)
        assert reconcile._reconcile_overnight_stops(db) == 0 and p.status == "OPEN"

    def test_filled_stop_closes_the_position(self, env):
        db, b, _ = env
        p = self.carried(db, qty=10, entry=100.0)
        b.plain_orders = [stop_row("S1", "TRADED", 10, 96.0)]
        assert reconcile._reconcile_overnight_stops(db) == 1
        assert p.status == "STOP_HIT" and p.realized_pnl == pytest.approx(-40.0)

    def test_settle_error_is_isolated_per_position(self, env, monkeypatch):
        db, b, _ = env
        bad = self.carried(db, overnight_stop_order_id="S1")
        good = self.carried(db, overnight_stop_order_id="S2")
        b.plain_orders = [stop_row("S1", "TRADED", 10, 96.0), stop_row("S2", "TRADED", 10, 96.0)]
        real = reconcile.overnight_stop.settle_stop_row

        def flaky(db_, pos, row):
            if pos.id == bad.id:
                raise ValueError("boom")
            return real(db_, pos, row)
        monkeypatch.setattr(reconcile.overnight_stop, "settle_stop_row", flaky)
        assert reconcile._reconcile_overnight_stops(db) == 1
        assert good.status == "STOP_HIT" and bad.status == "OPEN"


# ── run_exit_reconciliation ──────────────────────────────────────────────────
class TestRunExitReconciliation:
    def test_no_positions_and_no_stops_is_a_cheap_noop(self, env):
        db, b, _ = env
        assert reconcile.run_exit_reconciliation(db) == 0
        assert b.of("get_super_order_list") == []

    def test_target_hit_books_price_pnl_capital_and_lock(self, env):
        db, b, sent = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=100.0, target=("TRADED", 102.0, 102.0), stop=("PENDING", 99.0))]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "TARGET_HIT" and p.exit_price == 102.0
        assert p.realized_pnl == pytest.approx(20.0) and p.realized_pnl_pct == pytest.approx(2.0)
        assert p.dhan_exit_order_id == "SO1" and p.closed_at is not None and p.error_message is None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 1_000.0 + 20.0)
        assert state(db)["realized_pnl_today"] == pytest.approx(20.0)
        assert not lock_held(db, p.symbol)
        assert "🟢" in sent["info"][0] and "TARGET_HIT" in sent["info"][0]

    def test_stop_hit_books_a_loss(self, env):
        db, b, sent = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=100.0, target=("PENDING", 102.0), stop=("TRADED", 99.0, 98.9))]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "STOP_HIT" and p.exit_price == 98.9
        assert p.realized_pnl == pytest.approx(-11.0)
        assert "🔴" in sent["info"][0]

    def test_target_wins_if_both_legs_show_filled(self, env):
        db, b, _ = env
        p = mkpos(db, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=100.0, target=("TRADED", 102.0), stop=("TRADED", 99.0))]
        reconcile.run_exit_reconciliation(db)
        assert p.status == "TARGET_HIT"

    def test_missing_fill_price_uses_the_positions_own_trigger_never_zero(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="PENDING", target=("PENDING",), stop=("TRADED",))]
        reconcile.run_exit_reconciliation(db)
        assert p.status == "STOP_HIT" and p.exit_price == pytest.approx(99.0)   # its own stop_price
        assert p.realized_pnl == pytest.approx(-10.0)                            # NOT -1,000

    def test_hit_leg_order_id_is_recorded_falling_back_to_the_super_id(self, env):
        db, b, _ = env
        p = mkpos(db, super_id="SO1")
        row = super_row("SO1", avg=100.0, target=("TRADED", 102.0))
        row["legDetails"][0]["orderId"] = "LEG77"
        b.super_orders = [row]
        reconcile.run_exit_reconciliation(db)
        assert p.dhan_exit_order_id == "LEG77"

    def test_super_order_not_in_the_book_yet_is_skipped(self, env):
        db, b, _ = env
        p = mkpos(db, super_id="SO1")
        b.super_orders = [super_row("OTHER", target=("TRADED", 102.0))]
        assert reconcile.run_exit_reconciliation(db) == 0 and p.status == "OPEN"

    def test_position_without_a_super_order_id_is_not_visible_to_this_pass(self, env):
        db, b, _ = env
        mkpos(db, super_id=None)
        assert reconcile.run_exit_reconciliation(db) == 0
        assert b.of("get_super_order_list") == []

    def test_super_order_fetch_failure_returns_only_overnight_closures(self, env):
        db, b, _ = env
        mkpos(db, super_id="SO1")
        carried = mkpos(db, overnight_converted_to_cnc=True, overnight_stop_order_id="S1")
        b.plain_orders = [stop_row("S1", "TRADED", 10, 96.0)]
        b.super_error = RuntimeError("down")
        assert reconcile.run_exit_reconciliation(db) == 1
        assert carried.status == "STOP_HIT"

    def test_overnight_closure_counts_even_when_nothing_else_is_open(self, env):
        db, b, _ = env
        mkpos(db, overnight_converted_to_cnc=True, overnight_stop_order_id="S1")
        b.plain_orders = [stop_row("S1", "TRADED", 10, 96.0)]
        assert reconcile.run_exit_reconciliation(db) == 1

    def test_overnight_and_super_closures_are_summed(self, env):
        db, b, _ = env
        mkpos(db, overnight_converted_to_cnc=True, overnight_stop_order_id="S1")
        mkpos(db, super_id="SO1")
        b.plain_orders = [stop_row("S1", "TRADED", 10, 96.0)]
        b.super_orders = [super_row("SO1", avg=100.0, target=("TRADED", 102.0))]
        assert reconcile.run_exit_reconciliation(db) == 2

    def test_stuck_sweep_crash_does_not_stop_the_pass(self, env, monkeypatch):
        db, b, _ = env
        p = mkpos(db, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=100.0, target=("TRADED", 102.0))]
        monkeypatch.setattr(reconcile, "resolve_stuck_pending",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sweep boom")))
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "TARGET_HIT"

    # ── entry-fill correction ──
    def test_real_entry_fill_corrects_price_capital_ledger_and_rearms_legs(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")                  # capital 1,000
        b.super_orders = [super_row("SO1", avg=101.0, target=("PENDING", 102.0), stop=("PENDING", 99.0))]
        assert reconcile.run_exit_reconciliation(db) == 0
        assert p.entry_price == 101.0 and p.capital_risked == pytest.approx(1_010.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE - 10.0)        # delta pushed through the ledger
        mods = b.of("modify_super_order")
        assert mods[0] == {"order_id": "SO1", "order_leg": "TARGET_LEG", "target_price": 103.02}
        assert mods[1] == {"order_id": "SO1", "order_leg": "STOP_LOSS_LEG", "stop_loss_price": 99.99}
        assert p.target_price == 103.02 and p.stop_price == 99.99

    def test_entry_correction_is_idempotent(self, env):
        db, b, _ = env
        mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=101.0, target=("PENDING", 102.0), stop=("PENDING", 99.0))]
        reconcile.run_exit_reconciliation(db)
        n, cap = len(b.of("modify_super_order")), available(db)
        reconcile.run_exit_reconciliation(db)
        assert len(b.of("modify_super_order")) == n and available(db) == pytest.approx(cap)

    def test_flat_sell_placeholder_follows_the_entry_correction_but_legs_are_not_rearmed(self, env):
        db, b, _ = env
        p = pending(db, entry=100.0, qty=10, super_id="SO1")           # placeholder exit == entry
        b.super_orders = [super_row("SO1", avg=101.0)]
        reconcile.run_exit_reconciliation(db)
        assert p.entry_price == 101.0 and p.exit_price == 101.0         # phantom P&L stays exactly 0
        assert b.of("modify_super_order") == []                          # nothing to re-arm on a closing position

    def test_cheaper_real_fill_returns_the_excess_capital(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=99.0, target=("PENDING", 102.0), stop=("PENDING", 98.0))]
        reconcile.run_exit_reconciliation(db)
        assert p.capital_risked == pytest.approx(990.0) and available(db) == pytest.approx(LEDGER_AVAILABLE + 10.0)

    def test_rearm_failure_keeps_the_correction_and_is_not_fatal(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.modify_error = RuntimeError("leg already filled")
        b.super_orders = [super_row("SO1", avg=101.0, target=("PENDING", 102.0), stop=("PENDING", 99.0))]
        reconcile.run_exit_reconciliation(db)
        assert p.entry_price == 101.0 and p.target_price == pytest.approx(102.0)   # legs untouched

    def test_no_rearm_when_super_orders_are_disabled(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "USE_SUPER_ORDER", False)
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=101.0, target=("PENDING", 102.0), stop=("PENDING", 99.0))]
        reconcile.run_exit_reconciliation(db)
        assert p.entry_price == 101.0 and b.of("modify_super_order") == []

    @pytest.mark.parametrize("avg", [100.0, 100.0000001, None, 0, "junk"])
    def test_no_correction_when_fill_is_unknown_or_the_same(self, env, avg):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=avg, target=("PENDING", 102.0), stop=("PENDING", 99.0))]
        reconcile.run_exit_reconciliation(db)
        assert p.entry_price == 100.0 and b.of("modify_super_order") == []
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_unfilled_entry_is_not_used_for_correction(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, super_id="SO1")
        b.super_orders = [super_row("SO1", status="PENDING", avg=105.0)]
        reconcile.run_exit_reconciliation(db)
        assert p.entry_price == 100.0

    # ── dead exit legs / dead entry ──
    # 2026-09-21 fix (session80 — REFEX/LLOYDSENT sync gap): flagging now
    # requires EVERY exit leg that exists on the order to be dead, not just
    # one — see the detection comment in orders/reconcile.py. So both cases
    # below use two dead legs.
    @pytest.mark.parametrize("target,stop", [
        (("REJECTED", 102.0), ("CANCELLED", 99.0)),
        (("EXPIRED", 102.0), ("REJECTED", 99.0)),
    ])
    def test_dead_exit_legs_flag_the_position_for_a_plain_sell(self, env, target, stop):
        db, b, _ = env
        p = mkpos(db, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=100.0, target=target, stop=stop)]
        assert reconcile.run_exit_reconciliation(db) == 0
        assert p.status == "EXIT_LEGS_REJECTED" and "plain MARKET SELL" in p.error_message
        assert lock_held(db, p.symbol) and available(db) == pytest.approx(LEDGER_AVAILABLE)

    @pytest.mark.parametrize("target,stop", [
        (("REJECTED", 102.0), ("PENDING", 99.0)),
        (("PENDING", 102.0), ("CANCELLED", 99.0)),
    ])
    def test_single_dead_leg_does_not_flag_while_the_other_leg_is_still_live(self, env, target, stop):
        """The exact REFEX/LLOYDSENT scenario the session80 fix addressed:
        only one exit leg had been rejected while the other was still
        resting normally at the broker. The position must stay OPEN (and
        keep being polled) so the live leg's eventual real fill is still
        caught by the normal hit_kind detection."""
        db, b, _ = env
        p = mkpos(db, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=100.0, target=target, stop=stop)]
        assert reconcile.run_exit_reconciliation(db) == 0
        assert p.status == "OPEN" and p.error_message is None

    @pytest.mark.parametrize("dead", ["REJECTED", "CANCELLED"])
    def test_dead_entry_releases_capital_and_lock_without_pnl(self, env, dead):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status=dead)]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "ERROR" and dead in p.error_message and p.closed_at is not None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 1_000.0)
        assert state(db)["realized_pnl_today"] == 0.0 and not lock_held(db, p.symbol)

    def test_dead_entry_without_a_leg_name_is_still_recognised(self, env):
        db, b, _ = env
        p = mkpos(db, super_id="SO1")
        row = super_row("SO1", status="REJECTED")
        row.pop("legName", None)
        b.super_orders = [row]
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "ERROR"

    def test_dead_status_on_a_non_entry_row_is_ignored(self, env):
        db, b, _ = env
        p = mkpos(db, super_id="SO1")
        b.super_orders = [super_row("SO1", status="REJECTED", legName="STOP_LOSS_LEG")]
        assert reconcile.run_exit_reconciliation(db) == 0 and p.status == "OPEN"

    def test_entry_still_working_changes_nothing(self, env):
        db, b, _ = env
        p = mkpos(db, super_id="SO1")
        b.super_orders = [super_row("SO1", status="TRANSIT")]
        assert reconcile.run_exit_reconciliation(db) == 0
        assert p.status == "OPEN" and available(db) == pytest.approx(LEDGER_AVAILABLE)

    # ── flat-sell placeholders through the full pass ──
    def test_flat_sell_with_exit_order_id_is_resolved_via_the_order_list(self, env):
        db, b, _ = env
        p = pending(db, entry=100.0, qty=10, exit_order_id="X1", super_id="SO1")
        b.plain_orders = [plain_row("X1", avg=101.0)]
        b.super_orders = [super_row("SO1", avg=100.0)]
        assert reconcile.run_exit_reconciliation(db) == 0          # resolutions are not "closed" positions
        assert p.exit_price == 101.0 and p.realized_pnl == pytest.approx(10.0) and p.error_message is None
        assert p.status == "EOD_SQUAREOFF"

    def test_overnight_carried_flat_sell_without_super_id_is_resolved(self, env):
        # audit fix 2026-09-19: dhan_super_order_id is NULL after CNC conversion
        db, b, _ = env
        p = pending(db, entry=100.0, qty=10, exit_order_id="X1", super_id=None,
                    overnight_converted_to_cnc=True)
        b.plain_orders = [plain_row("X1", avg=98.0)]
        reconcile.run_exit_reconciliation(db)
        assert p.exit_price == 98.0 and p.realized_pnl == pytest.approx(-20.0)

    def test_flat_sell_without_order_id_keeps_the_placeholder_and_clears_the_sentinel(self, env):
        db, b, _ = env
        p = pending(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", avg=100.0)]
        reconcile.run_exit_reconciliation(db)
        assert p.error_message is None and p.exit_price == 100.0 and p.status == "EOD_SQUAREOFF"

    def test_dead_flat_sell_goes_to_error_and_is_not_reprocessed(self, env):
        db, b, sent = env
        p = pending(db, exit_order_id="X1", super_id="SO1")
        b.plain_orders = [plain_row("X1", status="REJECTED")]
        b.super_orders = [super_row("SO1", avg=100.0, target=("TRADED", 102.0))]
        reconcile.run_exit_reconciliation(db)
        assert p.status == "ERROR" and len(sent["critical"]) == 1
        assert p.exit_price == 100.0                                # the stray TARGET_LEG did not re-close it


# ── retention ────────────────────────────────────────────────────────────────
class TestRetentionCleanup:
    def old(self, db, status, days=4, **kw):
        return mkpos(db, status=status, closed_at=datetime.now(timezone.utc) - timedelta(days=days),
                     claim_lock=False, **kw)

    @pytest.mark.parametrize("status", ["TARGET_HIT", "STOP_HIT", "EOD_SQUAREOFF", "MANUAL_EXIT",
                                        "STAGNATION_EXIT", "ERROR"])
    def test_old_terminal_rows_are_deleted(self, env, status):
        db, _, _ = env
        self.old(db, status)
        assert reconcile.run_retention_cleanup(db) == 1
        assert db.query(models.ScalpPosition).count() == 0

    def test_recent_rows_are_kept(self, env):
        db, _, _ = env
        self.old(db, "TARGET_HIT", days=2)
        assert reconcile.run_retention_cleanup(db) == 0

    @pytest.mark.parametrize("status", ["OPEN", "EXIT_LEGS_REJECTED"])
    def test_live_exposure_is_never_deleted_however_old(self, env, status):
        db, _, _ = env
        self.old(db, status, days=400)
        assert reconcile.run_retention_cleanup(db) == 0
        assert db.query(models.ScalpPosition).count() == 1

    def test_rows_without_closed_at_age_by_opened_at(self, env):
        db, _, _ = env
        p = mkpos(db, status="ERROR", claim_lock=False)
        p.closed_at = None
        p.opened_at = datetime.now(timezone.utc) - timedelta(days=10)
        db.commit()
        assert reconcile.run_retention_cleanup(db) == 1

    def test_unclosed_recent_error_row_is_kept(self, env):
        db, _, _ = env
        p = mkpos(db, status="ERROR", claim_lock=False)
        p.closed_at = None
        db.commit()
        assert reconcile.run_retention_cleanup(db) == 0

    def test_only_the_expired_rows_go(self, env):
        db, _, _ = env
        self.old(db, "STOP_HIT", days=5)
        keep = self.old(db, "STOP_HIT", days=1)
        assert reconcile.run_retention_cleanup(db) == 1
        assert [p.id for p in db.query(models.ScalpPosition).all()] == [keep.id]

    def test_retention_window_follows_config(self, env, monkeypatch):
        db, _, _ = env
        monkeypatch.setattr(config, "TRADE_HISTORY_RETENTION_DAYS", 10.0)
        self.old(db, "STOP_HIT", days=5)
        assert reconcile.run_retention_cleanup(db) == 0


# ── Regression for the bug found (and fixed) by the 2026-09-21 audit ────────
class TestAuditRegressions:
    def test_stop_or_target_filling_on_a_flat_sell_placeholder_should_not_release_capital_twice(self, env):
        db, b, _ = env
        p = pending(db, entry=100.0, qty=10, super_id="SO1", claim_lock=False)      # capital_risked 1,000
        # eod_squareoff.py already released capital when it placed the SELL:
        led = ledger._get_or_create(db)
        led.available_capital = LEDGER_AVAILABLE + 1_000.0
        db.commit()
        b.super_orders = [super_row("SO1", avg=100.0, target=("TRADED", 102.0, 102.0))]
        reconcile.run_exit_reconciliation(db)
        assert p.status == "TARGET_HIT"
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 1_000.0 + 20.0)    # capital once + the P&L
