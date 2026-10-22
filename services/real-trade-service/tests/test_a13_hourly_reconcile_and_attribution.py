"""A13 tests: hourly broker-vs-book drift check (execution/hourly_reconcile.py) and the attribution fallback in trade_records.py.

    cd services/real-trade-service
    python -m pytest tests/test_a13_hourly_reconcile_and_attribution.py -v

All tests are pure (no DB, no broker).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from execution import hourly_reconcile as hr
import trade_records as tr

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)


def _book(sym, qty, young=False, status="OPEN"):
    return {"symbol": sym, "qty": qty, "status": status, "young": young}


def test_broker_qty_map_takes_largest_of_holdings_and_positions():
    m = hr.broker_qty_map([{"tradingSymbol": "tcs", "totalQty": 5}], [{"tradingSymbol": "TCS", "netQty": 8}, {"tradingSymbol": "INFY", "buyQty": 3}])
    assert m == {"TCS": 8, "INFY": 3}


def test_broker_qty_map_ignores_junk_rows():
    assert hr.broker_qty_map([None, {"totalQty": 4}, {"tradingSymbol": "X", "totalQty": "bad"}], None) == {}


def test_missing_at_broker_and_short_are_warnings():
    d = hr.compute_drift([_book("AAA", 10), _book("BBB", 10)], {"BBB": 4}, [])
    kinds = {(x["kind"], x["symbol"]) for x in d}
    assert kinds == {("missing_at_broker", "AAA"), ("book_over_broker", "BBB")}
    assert all(x["severity"] == "warn" for x in d)


def test_young_position_is_skipped():
    assert hr.compute_drift([_book("AAA", 10, young=True)], {}, []) == []


def test_broker_surplus_and_unknown_symbols_are_info_only():
    d = hr.compute_drift([_book("AAA", 5)], {"AAA": 9, "ZZZ": 2}, [])
    assert {x["kind"] for x in d} == {"broker_over_book", "broker_only"}
    assert all(x["severity"] == "info" for x in d)
    assert hr.signature(d) == ""


def test_matching_book_is_clean():
    assert hr.compute_drift([_book("AAA", 5)], {"AAA": 5}, []) == []


def test_two_book_rows_same_symbol_are_summed():
    assert hr.compute_drift([_book("AAA", 3), _book("AAA", 2)], {"AAA": 5}, []) == []


def test_stale_order_is_a_warning_and_in_signature():
    d = hr.compute_drift([], {}, [{"id": 7, "symbol": "AAA", "side": "SELL", "status": "PLACED", "age_min": 200}])
    assert d[0]["kind"] == "stale_order" and "stale_order:AAA" in hr.signature(d)


def test_alert_action_new_repeat_and_clear():
    sig = "missing_at_broker:AAA:10:0:"
    assert hr.alert_action(sig, None, None, NOW) == "alert"
    assert hr.alert_action(sig, sig, NOW, NOW + timedelta(hours=1)) is None          # same drift, inside 4 h
    assert hr.alert_action(sig, sig, NOW, NOW + timedelta(hours=4)) == "alert"       # persists: re-alert
    assert hr.alert_action(sig + "|x", sig, NOW, NOW + timedelta(minutes=5)) == "alert"   # drift changed
    assert hr.alert_action("", sig, NOW, NOW + timedelta(hours=1)) == "clear"
    assert hr.alert_action("", None, None, NOW) is None


# ── attribution fallback ──────────────────────────────────────────────────────────────────────────────────────────────
def test_attribution_prefers_position_then_order_then_candidate():
    pos = NS(source_tab=None, entry_decision_label=None, entry_conviction_score=None)
    order = NS(source_tab="hot_picks", entry_decision_label=None, entry_conviction_score=None, execution_source="AUTO")
    cand = NS(source_tab="market_scan", decision_label="BUY NOW", conviction_score=72.5)
    a = tr._attribution(pos, order, cand, None)
    assert a["source_tab"] == "hot_picks" and a["decision_label"] == "BUY NOW" and a["conviction"] == 72.5
    assert a["attribution_source"] == {"source_tab": "order", "decision_label": "candidate"}
    pos2 = NS(source_tab="surprise", entry_decision_label="BUY", entry_conviction_score=60)
    a2 = tr._attribution(pos2, order, cand, None)
    assert a2["source_tab"] == "surprise" and a2["decision_label"] == "BUY" and a2["attribution_source"]["source_tab"] == "position"


def test_attribution_manual_entry_is_labelled_and_unknown_stays_none():
    pos = NS(source_tab=None, entry_decision_label=None, entry_conviction_score=None)
    manual = NS(source_tab=None, entry_decision_label=None, entry_conviction_score=None, execution_source="MANUAL")
    a = tr._attribution(pos, manual, None, None)
    assert a["source_tab"] == "manual" and a["decision_label"] is None and a["conviction"] is None
    b = tr._attribution(pos, None, None, None)
    assert b["source_tab"] is None


def test_attribution_never_raises_on_garbage():
    assert tr._attribution(object(), object(), object(), object())["source_tab"] is None


def test_attribution_coverage_counts_and_percent():
    recs = [{"origin": "system", "source_tab": "a", "decision_label": "X", "stop_basis": None, "imbalance_5": None, "tier_name": None},
            {"origin": "system", "source_tab": None, "decision_label": None, "stop_basis": "atr", "imbalance_5": 0.2, "tier_name": "t"},
            {"origin": "imported", "source_tab": None, "decision_label": None, "stop_basis": None, "imbalance_5": None, "tier_name": None}]
    c = tr.attribution_coverage(recs)
    assert c["all"]["source_tab"] == {"filled": 1, "total": 3, "pct": 33.3}
    assert c["system_entered"]["stop_basis"] == {"filled": 1, "total": 2, "pct": 50.0}
    assert tr.attribution_coverage([])["all"]["source_tab"]["pct"] is None
