"""Cumulative brokerage / charges ledger (2026-10-08, group 258).

Why this exists: orders/reconcile.py::run_retention_cleanup() deletes closed scalp_positions after
TRADE_HISTORY_RETENTION_DAYS (3), so "brokerage since the start" cannot be a SUM over scalp_positions.
Every settled closed position is therefore booked ONCE into scalp_charges_ledger (PK = position id, so
booking is idempotent) before it can be deleted; the totals are read from that table.

Rate card = the one the dashboard Charges tab uses for this service (all orders are INTRADAY):
brokerage Rs 20 or 0.03% per leg (whichever is lower), STT 0.025% both sides, exchange 0.00345%, SEBI
0.0001%, GST 18% on brokerage + exchange, stamp 0.003% on the buy value. Brokerage rate and cap are env
overridable (CHARGES_BROKERAGE_PCT / CHARGES_BROKERAGE_CAP_RS). Estimates, not contract-note figures.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

import config
from models import ScalpChargesLedger, ScalpPosition
from orders import trade_stats
from tz_utils import as_aware, ist_today_str

logger = logging.getLogger("position-stocks-charges-ledger")

_STT_INTRA_PCT = 0.025 / 100
_EXCHANGE_PCT = 0.00345 / 100
_SEBI_PCT = 0.0001 / 100
_GST = 0.18
_STAMP_INTRA_PCT = 0.003 / 100


def leg_brokerage(value: float) -> float:
    """Brokerage of ONE executed leg: the lower of the flat cap and pct of value (0 for an empty leg)."""
    if not value or value <= 0:
        return 0.0
    return min(value * (config.CHARGES_BROKERAGE_PCT / 100.0), config.CHARGES_BROKERAGE_CAP_RS)


def charges_for_trade(entry_price: float, exit_price: float, qty: int) -> dict:
    """Round-trip charges of one intraday buy+sell. Mirrors the frontend calcCharges() for this tab."""
    buy_value = max(0.0, float(entry_price or 0) * int(qty or 0))
    sell_value = max(0.0, float(exit_price or 0) * int(qty or 0))
    turnover = buy_value + sell_value
    brokerage = leg_brokerage(buy_value) + leg_brokerage(sell_value)
    stt = turnover * _STT_INTRA_PCT
    exchange = turnover * _EXCHANGE_PCT
    sebi = turnover * _SEBI_PCT
    gst = (brokerage + exchange) * _GST
    stamp = buy_value * _STAMP_INTRA_PCT
    return {
        "buy_value": buy_value, "sell_value": sell_value, "brokerage": brokerage,
        "gst_on_brokerage": brokerage * _GST,
        "total": brokerage + stt + exchange + sebi + gst + stamp,
    }


def _bookable(row) -> bool:
    qty = getattr(row, "quantity", None)
    entry = getattr(row, "entry_price", None)
    exit_ = getattr(row, "exit_price", None)
    return bool(trade_stats.is_settled(row) and qty and qty > 0 and entry and entry > 0 and exit_ and exit_ > 0)


def book_positions(db, rows: Iterable) -> int:
    """Book every settled row not yet in the ledger. Returns the number of NEW ledger rows. Never raises:
    a ledger failure must not block the retention cleanup or a dashboard read (callers just log)."""
    try:
        candidates = [r for r in rows if _bookable(r)]
        if not candidates:
            return 0
        ids = [r.id for r in candidates]
        already = set()
        for i in range(0, len(ids), 500):
            already.update(
                pid for (pid,) in db.query(ScalpChargesLedger.position_id)
                .filter(ScalpChargesLedger.position_id.in_(ids[i:i + 500])).all()
            )
        n = 0
        for r in candidates:
            if r.id in already:
                continue
            c = charges_for_trade(r.entry_price, r.exit_price, r.quantity)
            closed = as_aware(r.closed_at or r.opened_at)
            db.add(ScalpChargesLedger(
                position_id=r.id, symbol=r.symbol,
                day=ist_today_str(closed) if closed is not None else ist_today_str(),
                quantity=int(r.quantity), buy_value=round(c["buy_value"], 2), sell_value=round(c["sell_value"], 2),
                brokerage=round(c["brokerage"], 2), gst_on_brokerage=round(c["gst_on_brokerage"], 2),
                total_charges=round(c["total"], 2), gross_pnl=r.realized_pnl,
            ))
            n += 1
        if n:
            db.commit()
        return n
    except Exception:
        logger.exception("charges ledger: booking failed; totals may lag until the next run")
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def sync_all(db) -> int:
    """Book every closed position currently in scalp_positions (cheap: one query + one IN lookup)."""
    try:
        rows = db.query(ScalpPosition).filter(ScalpPosition.status.notin_(("OPEN", "EXIT_LEGS_REJECTED"))).all()
    except Exception:
        logger.exception("charges ledger: could not read scalp_positions")
        return 0
    return book_positions(db, rows)


def cumulative(db, recent_days: int = 14) -> dict:
    """Totals since the first booked trade, plus a per-day list (newest first) of the last `recent_days` days."""
    sync_all(db)
    rows = db.query(ScalpChargesLedger).all()
    by_day: dict = defaultdict(lambda: {"trades": 0, "brokerage": 0.0, "total_charges": 0.0})
    for r in rows:
        d = by_day[r.day]
        d["trades"] += 1
        d["brokerage"] += r.brokerage or 0.0
        d["total_charges"] += r.total_charges or 0.0
    trades = len(rows)
    brokerage = sum(r.brokerage or 0.0 for r in rows)
    gst_b = sum(r.gst_on_brokerage or 0.0 for r in rows)
    total = sum(r.total_charges or 0.0 for r in rows)
    turnover = sum((r.buy_value or 0.0) + (r.sell_value or 0.0) for r in rows)
    gross = sum(r.gross_pnl or 0.0 for r in rows)
    days = sorted(by_day.keys(), reverse=True)
    return {
        "since": min(by_day) if by_day else None,
        "trading_days": len(by_day),
        "trades": trades,
        "brokerage_total": round(brokerage, 2),
        "brokerage_incl_gst": round(brokerage + gst_b, 2),
        "all_charges_total": round(total, 2),
        "turnover": round(turnover, 2),
        "gross_pnl_total": round(gross, 2),
        "avg_brokerage_per_trade": round(brokerage / trades, 2) if trades else None,
        "brokerage_pct_of_gross_pnl": round(100.0 * brokerage / gross, 1) if gross > 0 else None,
        "rate_card": {"pct": config.CHARGES_BROKERAGE_PCT, "cap_rs": config.CHARGES_BROKERAGE_CAP_RS},
        "recent_days": [
            {"day": d, "trades": by_day[d]["trades"], "brokerage": round(by_day[d]["brokerage"], 2),
             "total_charges": round(by_day[d]["total_charges"], 2)}
            for d in days[: max(1, min(int(recent_days), 90))]
        ],
        "note": ("Counted from when this ledger was first deployed; trades deleted by the 3-day history "
                 "retention before that are not included. Estimates from the Charges-tab rate card."),
    }
