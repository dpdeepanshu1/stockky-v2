"""
tests/test_overnight_stop_coverage.py

Closes the coverage gaps left in orders/overnight_stop.py after the
session112 notify_fire_and_forget round (78%, 52 missing: 67-68, 82-91,
103, 106, 109, 174, 176-178, 183-185, 225, 234-236, 244-246, 273, 279-281,
284, 294, 297-299, 304, 311, 322, 335, 338-340, 346, 355-356, 373-374,
377-380).

tests/test_overnight_stop.py already exercises the main partial/complete
fill accounting end to end (via reconcile._reconcile_overnight_stops and
overnight_stop.morning_recheck for the common LIVE/IN_FLIGHT/re-arm paths).
What's missing is almost entirely defensive branches: malformed broker rows
(row_avg_price / row_cum_qty / _trade_qty / _trade_price exception paths),
edge-case inputs to the accounting primitives (delta_fill_price's
legacy-row and implausible-price fallbacks, _settle's no-cum-qty /
no-price / negative-delta early returns), the exception/early-return
guards in settle_from_trades / settle_before_flat_sell / rearm /
morning_recheck, and morning_recheck's own "closed via settle_stop_row",
"settle_before_flat_sell fallback to trade history", "unrecognized status",
and per-position exception-does-not-abort-loop branches.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_overnight_stop_coverage.py -q \\
        --cov=orders.overnight_stop --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import notifier
from capital import ledger
from execution import dhan_client
from orders import overnight_stop


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    sent = {"info": [], "critical": []}
    monkeypatch.setattr(notifier, "notify_sync", lambda m, *a, **k: sent["info"].append(m))
    monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda m, *a, **k: sent["info"].append(m))
    monkeypatch.setattr(notifier, "notify_critical", lambda m, *a, **k: sent["critical"].append(m))
    led = ledger._get_or_create(db)
    led.total_allocated_capital, led.available_capital = 100000.0, 90000.0
    db.commit()
    yield db, sent
    db.close()


def mkpos(db, qty=100, entry=100.0, stop_id="S1", converted=True,
          booked_qty=0, booked_notional=0.0, prior_qty=0):
    p = models.ScalpPosition(
        symbol="XYZ", window_source="5m", adaptive_target_pct=2.0, adaptive_stop_pct=1.0,
        status="OPEN", quantity=qty, entry_price=entry, capital_risked=entry * qty,
        overnight_converted_to_cnc=converted, overnight_stop_order_id=stop_id, dhan_security_id="123",
        target_price=entry * 1.02, stop_price=entry * 0.96, opened_at=datetime.now(timezone.utc),
        overnight_stop_filled_qty_so_far=booked_qty, overnight_stop_filled_notional_so_far=booked_notional,
        overnight_stop_prior_qty=prior_qty,
    )
    db.add(p)
    db.commit()
    return p


# ══════════════════════════════════════════════════════════════════════════════
# Row-parsing helpers — malformed / alternate-shaped broker rows
# ══════════════════════════════════════════════════════════════════════════════

class TestRowParsingEdges:
    """Lines 67-68, 82-91"""

    def test_row_avg_price_non_numeric_returns_none(self):
        # hits the except (TypeError, ValueError) branch — lines 67-68
        assert overnight_stop.row_avg_price({"averageTradedPrice": "not-a-number"}) is None

    def test_row_avg_price_zero_returns_none(self):
        assert overnight_stop.row_avg_price({"averageTradedPrice": 0}) is None

    def test_row_avg_price_negative_returns_none(self):
        assert overnight_stop.row_avg_price({"average_traded_price": -5.0}) is None

    def test_row_cum_qty_filled_qty_non_numeric_falls_back_to_total_minus_remaining(self):
        # primary key present but non-numeric → except pass → falls through to total-remaining
        # covers lines 82-83 (except pass) then 84-88 (total-remaining path)
        row = {"filledQty": "oops", "quantity": 100, "remainingQuantity": 30}
        assert overnight_stop.row_cum_qty(row) == 70

    def test_row_cum_qty_total_minus_remaining_non_numeric_returns_none(self):
        # total-remaining path, but both non-numeric — lines 87, 89-90
        row = {"quantity": "bad", "remainingQuantity": "also-bad"}
        assert overnight_stop.row_cum_qty(row) is None

    def test_row_cum_qty_only_total_present_returns_none(self):
        # remaining is None → condition on line 86 is False → line 91: return None
        assert overnight_stop.row_cum_qty({"quantity": 100}) is None

    def test_row_cum_qty_nothing_present_returns_none(self):
        # raw is None, total is None → line 91
        assert overnight_stop.row_cum_qty({}) is None

    def test_row_cum_qty_alt_key_qty_fallback(self):
        # Uses "qty" instead of "quantity" — exercises the ternary on line 84
        row = {"qty": 80, "remaining_quantity": 20}
        assert overnight_stop.row_cum_qty(row) == 60


# ══════════════════════════════════════════════════════════════════════════════
# delta_fill_price — legacy-row and implausible-price fallbacks
# ══════════════════════════════════════════════════════════════════════════════

class TestDeltaFillPriceEdges:
    """Lines 103, 106, 109"""

    def test_delta_qty_zero_returns_cumulative_average(self, env):
        # delta_qty = cum_qty - booked_qty = 100 - 100 = 0 → line 103: return cum_avg_price
        db, _ = env
        p = mkpos(db, booked_qty=100)
        assert overnight_stop.delta_fill_price(p, 95.0, 100) == 95.0

    def test_delta_qty_negative_returns_cumulative_average(self, env):
        # cum_qty < booked_qty → delta <= 0 → line 103
        db, _ = env
        p = mkpos(db, booked_qty=110)
        assert overnight_stop.delta_fill_price(p, 95.0, 100) == 95.0

    def test_legacy_row_booked_qty_but_no_notional_assumes_same_price(self, env):
        # booked_qty=40 and notional=0 → line 106: prev_notional = booked_qty * cum_avg_price
        # price = (96*100 - 40*96) / 60 = 96.0 exactly (same-price fallback)
        db, _ = env
        p = mkpos(db, booked_qty=40, booked_notional=0.0)
        result = overnight_stop.delta_fill_price(p, 96.0, 100)
        assert result == pytest.approx(96.0)

    def test_implausible_price_falls_back_to_cumulative_average(self, env):
        # notional deliberately corrupted so derived delta price is > cum_avg*3 → line 109
        # booked 50 shares at a notional of 50*1000=50000 (avg 1000/sh, nonsense).
        # derived delta price = (96*100 - 50000)/50 = (9600-50000)/50 = -807 → < cum_avg/3
        db, _ = env
        p = mkpos(db, booked_qty=50, booked_notional=50 * 1000.0)
        result = overnight_stop.delta_fill_price(p, 96.0, 100)
        assert result == pytest.approx(96.0)


# ══════════════════════════════════════════════════════════════════════════════
# _settle — early-return guards
# ══════════════════════════════════════════════════════════════════════════════

class TestSettleEarlyReturns:
    """Lines 174, 176-178, 183-185"""

    def test_cum_qty_none_is_a_no_op(self, env):
        # line 174: return result immediately
        db, _ = env
        p = mkpos(db)
        result = overnight_stop._settle(
            db, p, kind="partial", status="PART_TRADED", price=95.0, cum_qty=None
        )
        assert result == {"status": "PART_TRADED", "booked_qty": 0, "closed": False, "residual": False}
        assert p.quantity == 100

    def test_cum_qty_zero_is_a_no_op(self, env):
        # line 174 again (cum_qty <= 0)
        db, _ = env
        p = mkpos(db)
        result = overnight_stop._settle(
            db, p, kind="partial", status="PART_TRADED", price=95.0, cum_qty=0
        )
        assert result["booked_qty"] == 0
        assert p.quantity == 100

    def test_missing_price_with_positive_cum_qty_defers_to_next_pass(self, env, caplog):
        # lines 176-178: warning + return
        import logging
        db, _ = env
        p = mkpos(db)
        with caplog.at_level(logging.WARNING, logger="position-stocks-overnight-stop"):
            result = overnight_stop._settle(
                db, p, kind="partial", status="PART_TRADED", price=None, cum_qty=30
            )
        assert result["booked_qty"] == 0
        assert p.quantity == 100
        assert "no fill price" in caplog.text

    def test_negative_delta_is_not_booked(self, env, caplog):
        # booked_qty=50, cum_qty=30 → delta=-20 → lines 183-185
        import logging
        db, _ = env
        p = mkpos(db, booked_qty=50)
        with caplog.at_level(logging.WARNING, logger="position-stocks-overnight-stop"):
            result = overnight_stop._settle(
                db, p, kind="partial", status="PART_TRADED", price=95.0, cum_qty=30
            )
        assert result["booked_qty"] == 0
        assert "not booking a negative delta" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════
# settle_stop_row — unrecognized status (line 225)
# ══════════════════════════════════════════════════════════════════════════════

class TestSettleStopRowUnrecognized:
    """Line 225"""

    def test_unrecognized_status_returns_early_without_settling(self, env):
        db, _ = env
        p = mkpos(db)
        row = {
            "orderId": "S1", "orderStatus": "SOME_WEIRD_STATUS",
            "filledQty": 10, "averageTradedPrice": 95.0,
        }
        result = overnight_stop.settle_stop_row(db, p, row)
        assert result == {"status": "SOME_WEIRD_STATUS", "booked_qty": 0, "closed": False, "residual": False}
        assert p.quantity == 100  # nothing booked


# ══════════════════════════════════════════════════════════════════════════════
# _trade_qty / _trade_price — malformed trade rows (lines 234-236, 244-246)
# ══════════════════════════════════════════════════════════════════════════════

class TestTradeRowParsingEdges:
    """Lines 234-236, 244-246"""

    def test_trade_qty_non_numeric_value_falls_through_to_zero(self):
        # line 234-235 (except pass), then line 236 (return 0)
        assert overnight_stop._trade_qty({"tradedQuantity": "oops"}) == 0

    def test_trade_qty_no_recognized_key_returns_zero(self):
        # all keys absent → all branches skipped → line 236
        assert overnight_stop._trade_qty({"somethingElse": 5}) == 0

    def test_trade_price_non_numeric_value_falls_through_to_zero(self):
        # line 244-245 (except pass), then line 246 (return 0.0)
        assert overnight_stop._trade_price({"tradedPrice": "oops"}) == 0.0

    def test_trade_price_no_recognized_key_returns_zero(self):
        # all keys absent → line 246
        assert overnight_stop._trade_price({}) == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# settle_from_trades — no order id / no matching trades / lookup failure
# ══════════════════════════════════════════════════════════════════════════════

class TestSettleFromTradesEdges:
    """Lines 273, 279-281, 284"""

    def test_no_order_id_returns_empty_result(self, env):
        # line 273: early return when order_id is falsy
        db, _ = env
        p = mkpos(db, stop_id=None)
        result = overnight_stop.settle_from_trades(db, p)
        assert result == {"status": "HISTORY", "booked_qty": 0, "closed": False, "residual": False}

    def test_trade_history_lookup_failure_is_swallowed(self, env, monkeypatch, caplog):
        # lines 279-281: exception caught, warning logged, empty returned
        import logging
        db, _ = env
        p = mkpos(db)

        def boom(db_, f, t):
            raise RuntimeError("Dhan trade-history API down")

        monkeypatch.setattr(dhan_client, "get_trade_history", boom)
        with caplog.at_level(logging.WARNING, logger="position-stocks-overnight-stop"):
            result = overnight_stop.settle_from_trades(db, p)
        assert result["booked_qty"] == 0
        assert "trade-history lookup failed" in caplog.text

    def test_no_matching_trades_returns_empty(self, env, monkeypatch):
        # line 284: qty=0 from aggregate_trades → return empty
        db, _ = env
        p = mkpos(db)
        # Return trades for a DIFFERENT order id — no match for "S1"
        monkeypatch.setattr(
            dhan_client, "get_trade_history",
            lambda db_, f, t: [{"orderId": "OTHER", "tradedQuantity": 50, "tradedPrice": 95.0}],
        )
        result = overnight_stop.settle_from_trades(db, p)
        assert result == {"status": "HISTORY", "booked_qty": 0, "closed": False, "residual": False}


# ══════════════════════════════════════════════════════════════════════════════
# settle_before_flat_sell — early returns, order-list failure, and
#                           stop-not-in-list → fallback to trade history
# ══════════════════════════════════════════════════════════════════════════════

class TestSettleBeforeFlatSellEdges:
    """Lines 294, 297-299, 304"""

    def test_not_converted_to_cnc_returns_none_result(self, env):
        # line 294: not (converted AND stop_id) → return none
        db, _ = env
        p = mkpos(db, converted=False)
        result = overnight_stop.settle_before_flat_sell(db, p)
        assert result == {"status": None, "booked_qty": 0, "closed": False, "residual": False}

    def test_no_stop_order_id_returns_none_result(self, env):
        # line 294: converted=True but stop_id=None → same guard
        db, _ = env
        p = mkpos(db, stop_id=None)
        result = overnight_stop.settle_before_flat_sell(db, p)
        assert result["status"] is None

    def test_order_list_fetch_failure_is_swallowed(self, env, monkeypatch, caplog):
        # lines 297-299: exception from get_order_list caught, warning, return none
        import logging
        db, _ = env
        p = mkpos(db)

        def boom(db_):
            raise RuntimeError("Dhan order-list API down")

        monkeypatch.setattr(dhan_client, "get_order_list", boom)
        with caplog.at_level(logging.WARNING, logger="position-stocks-overnight-stop"):
            result = overnight_stop.settle_before_flat_sell(db, p)
        assert result["status"] is None
        assert "order-list fetch failed before flat SELL" in caplog.text

    def test_stop_not_in_order_list_falls_back_to_trade_history(self, env, monkeypatch):
        # line 304: row is None (order not in list) → settle_from_trades called
        db, _ = env
        p = mkpos(db, qty=100)
        # Order list returns nothing matching S1
        monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: [])
        # Trade history has full fill for S1
        monkeypatch.setattr(
            dhan_client, "get_trade_history",
            lambda db_, f, t: [{"orderId": "S1", "tradedQuantity": 100, "tradedPrice": 95.0}],
        )
        result = overnight_stop.settle_before_flat_sell(db, p)
        assert result["booked_qty"] == 100
        assert result["closed"] is True
        assert p.status == "STOP_HIT"


# ══════════════════════════════════════════════════════════════════════════════
# rearm — no quantity left, placement failure, and successful re-arm notify
# ══════════════════════════════════════════════════════════════════════════════

class TestRearmEdges:
    """Lines 311, 322"""

    def test_zero_quantity_position_is_not_rearmed(self, env):
        # line 311: quantity <= 0 → return None immediately
        db, _ = env
        p = mkpos(db, qty=0)
        assert overnight_stop.rearm(db, p, 4.0, reason="test") is None

    def test_placement_failure_notifies_critical_and_returns_none(self, env, monkeypatch):
        # rearm: _place_overnight_stop returns {} (no orderId) → new_id is falsy
        # → notify_critical fired with RE-ARM FAILED, returns None
        db, sent = env
        p = mkpos(db)
        monkeypatch.setattr(
            dhan_client, "place_cnc_stop_loss_market",
            lambda db_, **kw: {},   # no orderId → _place_overnight_stop returns None
        )
        result = overnight_stop.rearm(db, p, 4.0, reason="test failure path")
        assert result is None
        assert any("RE-ARM FAILED" in m for m in sent["critical"])
        assert p.overnight_stop_order_id == "S1"  # unchanged

    def test_successful_rearm_notifies_critical_and_returns_new_id(self, env, monkeypatch):
        # line 322: new_id is truthy → notify_critical with RE-ARMED message
        db, sent = env
        p = mkpos(db)
        monkeypatch.setattr(
            dhan_client, "place_cnc_stop_loss_market",
            lambda db_, **kw: {"orderId": "S_NEW"},
        )
        result = overnight_stop.rearm(db, p, 4.0, reason="missing from order book")
        assert result == "S_NEW"
        assert any("RE-ARMED" in m for m in sent["critical"])
        assert p.overnight_stop_order_id == "S_NEW"


# ══════════════════════════════════════════════════════════════════════════════
# morning_recheck — all edge branches
# ══════════════════════════════════════════════════════════════════════════════

class TestMorningRecheckEdges:
    """Lines 335, 338-340, 346, 355-356, 373-374, 377-380"""

    def test_no_carried_positions_returns_empty_summary(self, env):
        # line 335: positions list empty → return summary immediately
        db, _ = env
        summary = overnight_stop.morning_recheck(db, 4.0)
        assert summary == {"checked": 0, "live": 0, "rearmed": 0, "rearm_failed": 0, "closed": 0, "settled_qty": 0}

    def test_order_list_fetch_failure_returns_summary_untouched(self, env, monkeypatch):
        # lines 338-340: get_order_list raises → error logged → return summary (checked=0)
        db, _ = env
        mkpos(db)

        def boom(db_):
            raise RuntimeError("Dhan order-list API down")

        monkeypatch.setattr(dhan_client, "get_order_list", boom)
        summary = overnight_stop.morning_recheck(db, 4.0)
        assert summary["checked"] == 0

    def test_position_with_no_stop_order_id_is_skipped(self, env, monkeypatch):
        # line 346: stop_id is None → continue → checked stays 0
        db, _ = env
        mkpos(db, stop_id=None)
        monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: [])
        summary = overnight_stop.morning_recheck(db, 4.0)
        assert summary["checked"] == 0

    def test_missing_order_settled_from_trades_and_fully_closed(self, env, monkeypatch):
        # lines 355-356: settle_from_trades returns closed → summary["closed"] += 1, continue
        db, _ = env
        p = mkpos(db, qty=100)
        monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: [])
        monkeypatch.setattr(
            dhan_client, "get_trade_history",
            lambda db_, f, t: [{"orderId": "S1", "tradedQuantity": 100, "tradedPrice": 90.0}],
        )
        summary = overnight_stop.morning_recheck(db, 4.0)
        assert summary["closed"] == 1 and summary["settled_qty"] == 100
        assert p.status == "STOP_HIT" and p.quantity == 0

    def test_settle_stop_row_full_close_counts_as_closed(self, env, monkeypatch):
        # lines 373-374: res["closed"] is True → summary["closed"] += 1, continue
        db, _ = env
        p = mkpos(db, qty=100)
        row = {"orderId": "S1", "orderStatus": "TRADED", "filledQty": 100, "averageTradedPrice": 95.0}
        monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: [row])
        summary = overnight_stop.morning_recheck(db, 4.0)
        assert summary["closed"] == 1
        assert p.status == "STOP_HIT" and p.quantity == 0

    def test_unrecognized_status_is_logged_and_not_acted_on(self, env, monkeypatch, caplog):
        # lines 377-378: status not in any known set → warning logged, nothing done
        import logging
        db, _ = env
        mkpos(db)
        row = {"orderId": "S1", "orderStatus": "SOME_WEIRD_STATUS", "filledQty": 0, "averageTradedPrice": 0}
        monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: [row])
        with caplog.at_level(logging.WARNING, logger="position-stocks-overnight-stop"):
            summary = overnight_stop.morning_recheck(db, 4.0)
        assert summary["rearmed"] == 0 and summary["rearm_failed"] == 0 and summary["closed"] == 0
        assert "unrecognized status" in caplog.text

    def test_exception_processing_one_position_does_not_abort_the_pass(self, env, monkeypatch, caplog):
        # lines 379-380: exception inside the per-position try → error logged, loop continues
        import logging
        db, _ = env
        mkpos(db, stop_id="S1")
        mkpos(db, stop_id="S2")
        row1 = {"orderId": "S1", "orderStatus": "PENDING", "filledQty": 0, "averageTradedPrice": 0}
        row2 = {"orderId": "S2", "orderStatus": "PENDING", "filledQty": 0, "averageTradedPrice": 0}
        monkeypatch.setattr(dhan_client, "get_order_list", lambda db_: [row1, row2])

        orig_row_status = overnight_stop.row_status

        def boom_for_first(row):
            if row.get("orderId") == "S1":
                raise RuntimeError("boom mid-loop")
            return orig_row_status(row)

        monkeypatch.setattr(overnight_stop, "row_status", boom_for_first)

        with caplog.at_level(logging.ERROR, logger="position-stocks-overnight-stop"):
            summary = overnight_stop.morning_recheck(db, 4.0)
        assert "overnight stop recheck failed for" in caplog.text
        assert summary["live"] == 1  # p2 still processed despite p1 raising
