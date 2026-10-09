"""Cumulative brokerage / charges ledger (2026-10-08, group 258).

Why this exists: orders/reconcile.py::run_retention_cleanup() deletes closed scalp_positions after
TRADE_HISTORY_RETENTION_DAYS (3), so "brokerage since the start" cannot be a SUM over scalp_positions.
Every settled closed position is therefore booked ONCE into scalp_charges_ledger (PK = position id, so
booking is idempotent) before it can be deleted; the totals are read from that table.

Rate card (all orders are INTRADAY), read from config.py so there is ONE env-overridable card shared with
real-trade-service: brokerage Rs 20 or 0.03% per leg (whichever is lower), STT 0.025% on the SELL leg only,
exchange 0.00297% + IPFT 0.0001%, SEBI 0.0001%, GST 18% on brokerage + exchange + IPFT + SEBI (not on STT or stamp),
stamp 0.003% on the buy value. Estimates, not contract-note figures.

2026-10-08 (group 262): the old copy here charged STT on BOTH legs and used the pre-Oct-2024 exchange rate, and a row
was priced once at booking and never revisited. Now (a) the rates come from config, (b) every read RESTATES stored rows
from their own buy/sell values with the current card (history self-corrects after a rate fix) and (c) a row whose
position was later repaired (entry/exit price or P&L corrected from the order book) is refreshed while the position
still exists. Components (stt/exchange/sebi/gst/stamp) are exposed per period.

2026-10-09 (group 263): the split itself (stt / exchange / sebi / gst / stamp) and the net P&L are now STORED on every
ledger row, not only recomputed on read, and the stored values are re-aligned with the current rate card whenever
the ledger is read (restate_stored). cumulative() also returns a `pnl` block (gross - charges = net, all-time and
today, same shape as real-trade-service) and per-day gross / charges / net; trades() lists the per-trade rows.
sync_throttled() books newly settled trades from the fast reconcile loop, so nothing depends on someone opening the
dashboard (or on the 3-day retention job) before a trade reaches the ledger.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable, Optional

import config
from models import ScalpChargesLedger, ScalpPosition
from orders import trade_stats
from tz_utils import as_aware, ist_today_str

logger = logging.getLogger("position-stocks-charges-ledger")

_GST = config.GST_PCT / 100.0
_COMPONENTS = ("brokerage", "stt", "exchange", "sebi", "gst", "stamp")


def leg_brokerage(value: float) -> float:
    """Brokerage of ONE executed leg: the lower of the flat cap and pct of value (0 for an empty leg)."""
    if not value or value <= 0:
        return 0.0
    return min(value * (config.CHARGES_BROKERAGE_PCT / 100.0), config.CHARGES_BROKERAGE_CAP_RS)


def charges_for_values(buy_value: float, sell_value: float) -> dict:
    """Every Dhan charge of one intraday buy + sell, from the two leg values."""
    buy_value = max(0.0, float(buy_value or 0))
    sell_value = max(0.0, float(sell_value or 0))
    turnover = buy_value + sell_value
    brokerage = leg_brokerage(buy_value) + leg_brokerage(sell_value)
    stt = sell_value * (config.STT_INTRADAY_SELL_PCT / 100.0)                       # sell leg only
    exchange = turnover * ((config.EXCHANGE_TXN_PCT + config.IPFT_PCT) / 100.0)     # NSE + IPFT
    sebi = turnover * (config.SEBI_TURNOVER_PCT / 100.0)
    gst = (brokerage + exchange + sebi) * _GST                                      # not on STT / stamp duty
    stamp = buy_value * (config.STAMP_DUTY_BUY_PCT_INTRADAY / 100.0)                # buy leg only
    return {
        "buy_value": buy_value, "sell_value": sell_value, "brokerage": brokerage,
        "gst_on_brokerage": brokerage * _GST, "stt": stt, "exchange": exchange, "sebi": sebi, "gst": gst,
        "stamp": stamp, "total": brokerage + stt + exchange + sebi + gst + stamp,
    }


def charges_for_trade(entry_price: float, exit_price: float, qty: int) -> dict:
    """Round-trip charges of one intraday buy+sell (prices x quantity)."""
    q = int(qty or 0)
    return charges_for_values(float(entry_price or 0) * q, float(exit_price or 0) * q)


def _bookable(row) -> bool:
    qty = getattr(row, "quantity", None)
    entry = getattr(row, "entry_price", None)
    exit_ = getattr(row, "exit_price", None)
    return bool(trade_stats.is_settled(row) and qty and qty > 0 and entry and entry > 0 and exit_ and exit_ > 0)


def _store_split(row, c) -> None:
    """Write the per-component split + net P&L (group 263) of charges `c` onto ledger row `row`."""
    row.stt = round(c["stt"], 2)
    row.exchange = round(c["exchange"], 2)
    row.sebi = round(c["sebi"], 2)
    row.gst = round(c["gst"], 2)
    row.stamp = round(c["stamp"], 2)
    gross = getattr(row, "gross_pnl", None)
    row.net_pnl = round(float(gross) - c["total"], 2) if gross is not None else None


def _apply(row, r, c) -> None:
    """Write the computed figures of position `r` onto ledger row `row`."""
    row.quantity = int(r.quantity)
    row.buy_value = round(c["buy_value"], 2)
    row.sell_value = round(c["sell_value"], 2)
    row.brokerage = round(c["brokerage"], 2)
    row.gst_on_brokerage = round(c["gst_on_brokerage"], 2)
    row.total_charges = round(c["total"], 2)
    row.gross_pnl = r.realized_pnl
    _store_split(row, c)


def book_positions(db, rows: Iterable) -> int:
    """Book every settled row not yet in the ledger and REFRESH a booked row whose position changed since (a repair
    corrected its entry/exit price or P&L). Returns the number of NEW ledger rows. Never raises: a ledger failure must
    not block the retention cleanup or a dashboard read (callers just log)."""
    try:
        candidates = [r for r in rows if _bookable(r)]
        if not candidates:
            return 0
        ids = [r.id for r in candidates]
        existing = {}
        for i in range(0, len(ids), 500):
            for row in db.query(ScalpChargesLedger).filter(ScalpChargesLedger.position_id.in_(ids[i:i + 500])).all():
                existing[row.position_id] = row
        n = changed = 0
        for r in candidates:
            c = charges_for_trade(r.entry_price, r.exit_price, r.quantity)
            row = existing.get(r.id)
            if row is not None:
                if (abs((row.buy_value or 0.0) - c["buy_value"]) > 0.005
                        or abs((row.sell_value or 0.0) - c["sell_value"]) > 0.005
                        or abs((row.gross_pnl or 0.0) - (r.realized_pnl or 0.0)) > 0.005):
                    _apply(row, r, c)
                    changed += 1
                continue
            closed = as_aware(r.closed_at or r.opened_at)
            new = ScalpChargesLedger(
                position_id=r.id, symbol=r.symbol,
                day=ist_today_str(closed) if closed is not None else ist_today_str(),
            )
            _apply(new, r, c)
            db.add(new)
            n += 1
        if n or changed:
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


_last_sync_at = 0.0


def sync_throttled(db, min_interval: float = 60.0) -> int:
    """sync_all() at most once per `min_interval` seconds (called from the fast reconcile loop every few seconds).
    Returns the number of NEW ledger rows, 0 when throttled. Never raises."""
    global _last_sync_at
    now = time.monotonic()
    if _last_sync_at and now - _last_sync_at < min_interval:
        return 0
    _last_sync_at = now
    try:
        return sync_all(db)
    except Exception:
        logger.exception("charges ledger: throttled sync failed")
        return 0


def restate_stored(db, rows: Iterable) -> int:
    """Re-align the STORED split / totals / net P&L of ledger `rows` with the current rate card (they are derived
    only from the row's own buy / sell value + gross P&L). Fills the columns of rows booked before group 263 too.
    Returns the number of rows changed. Never raises."""
    try:
        changed = 0
        for r in rows:
            c = _restated(r)
            before = tuple(getattr(r, k, None) for k in ("stt", "exchange", "sebi", "gst", "stamp", "net_pnl"))
            was_total = getattr(r, "total_charges", None)
            was_brokerage = getattr(r, "brokerage", None)
            _store_split(r, c)
            r.brokerage = round(c["brokerage"], 2)
            r.gst_on_brokerage = round(c["gst_on_brokerage"], 2)
            r.total_charges = round(c["total"], 2)
            after = tuple(getattr(r, k, None) for k in ("stt", "exchange", "sebi", "gst", "stamp", "net_pnl"))
            if (before != after or was_total is None or abs((was_total or 0.0) - r.total_charges) > 0.005
                    or abs((was_brokerage or 0.0) - r.brokerage) > 0.005):
                changed += 1
        if changed:
            db.commit()
        return changed
    except Exception:
        logger.exception("charges ledger: restating stored rows failed")
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _restated(r) -> dict:
    """A ledger row's charges recomputed from its own stored leg values with the CURRENT rate card, so rows booked
    before a rate fix are corrected on read (nothing else in the row depends on the card)."""
    return charges_for_values(r.buy_value, r.sell_value)


def _period(rows: list, start, end) -> dict:
    """Totals over ledger rows whose day is within [start, end] (inclusive YYYY-MM-DD; None = open ended)."""
    sel = [r for r in rows if (start is None or r.day >= start) and (end is None or r.day <= end)]
    comp = {k: 0.0 for k in _COMPONENTS}
    gst_b = 0.0
    charges = 0.0
    for r in sel:
        c = _restated(r)
        for k in _COMPONENTS:
            comp[k] += c[k]
        gst_b += c["gst_on_brokerage"]
        charges += c["total"]
    gross = sum(r.gross_pnl or 0.0 for r in sel)
    return {
        "from": start, "to": end, "trades": len(sel),
        "brokerage": round(comp["brokerage"], 2), "brokerage_incl_gst": round(comp["brokerage"] + gst_b, 2),
        "all_charges": round(charges, 2), "gross_pnl": round(gross, 2), "net_pnl": round(gross - charges, 2),
        "components": {k: round(v, 2) for k, v in comp.items()},
    }


def cumulative(db, recent_days: int = 14, today: Optional[str] = None) -> dict:
    """Totals since the first booked trade, plus a per-day list (newest first) of the last `recent_days` days."""
    sync_all(db)
    rows = db.query(ScalpChargesLedger).all()
    restate_stored(db, rows)          # group 263: keep the stored split / net aligned with the current rate card
    by_day: dict = defaultdict(lambda: {"trades": 0, "brokerage": 0.0, "total_charges": 0.0, "gross_pnl": 0.0})
    restated = {r.position_id: _restated(r) for r in rows}
    for r in rows:
        d = by_day[r.day]
        d["trades"] += 1
        d["brokerage"] += restated[r.position_id]["brokerage"]
        d["total_charges"] += restated[r.position_id]["total"]
        d["gross_pnl"] += r.gross_pnl or 0.0
    trades = len(rows)
    brokerage = sum(c["brokerage"] for c in restated.values())
    gst_b = sum(c["gst_on_brokerage"] for c in restated.values())
    total = sum(c["total"] for c in restated.values())
    turnover = sum((r.buy_value or 0.0) + (r.sell_value or 0.0) for r in rows)
    gross = sum(r.gross_pnl or 0.0 for r in rows)
    days = sorted(by_day.keys(), reverse=True)
    since = min(by_day) if by_day else None
    today = today or ist_today_str()
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    total_p = _period(rows, None, None)
    total_p["from"], total_p["to"] = since, today
    today_p = _period(rows, today, today)
    return {
        "since": since,
        "today_date": today,
        # group 263: same shape as real-trade-service's `pnl` block - booked gross P&L minus the charges above. Scope =
        # the ledger (trades booked since it was first deployed), so gross and charges always cover the same trades.
        "pnl": {
            "realized_gross_total": total_p["gross_pnl"], "charges_total": total_p["all_charges"],
            "net_realized_total": total_p["net_pnl"],
            "realized_gross_today": today_p["gross_pnl"], "charges_today": today_p["all_charges"],
            "net_realized_today": today_p["net_pnl"],
        },
        # Three rows: first booked trade -> yesterday (date range), today, grand total.
        "history": _period(rows, since, yesterday) if since and since <= yesterday else None,
        "today": today_p,
        "total": total_p,
        "trading_days": len(by_day),
        "trades": trades,
        "brokerage_total": round(brokerage, 2),
        "brokerage_incl_gst": round(brokerage + gst_b, 2),
        "all_charges_total": round(total, 2),
        "turnover": round(turnover, 2),
        "gross_pnl_total": round(gross, 2),
        "avg_brokerage_per_trade": round(brokerage / trades, 2) if trades else None,
        "brokerage_pct_of_gross_pnl": round(100.0 * brokerage / gross, 1) if gross > 0 else None,
        "rate_card": {"pct": config.CHARGES_BROKERAGE_PCT, "cap_rs": config.CHARGES_BROKERAGE_CAP_RS,
                      "stt_sell_pct": config.STT_INTRADAY_SELL_PCT,
                      "exchange_pct": round(config.EXCHANGE_TXN_PCT + config.IPFT_PCT, 6),
                      "sebi_pct": config.SEBI_TURNOVER_PCT, "gst_pct": config.GST_PCT,
                      "stamp_buy_pct": config.STAMP_DUTY_BUY_PCT_INTRADAY},
        "recent_days": [
            {"day": d, "trades": by_day[d]["trades"], "brokerage": round(by_day[d]["brokerage"], 2),
             "total_charges": round(by_day[d]["total_charges"], 2),
             "gross_pnl": round(by_day[d]["gross_pnl"], 2),
             "net_pnl": round(by_day[d]["gross_pnl"] - by_day[d]["total_charges"], 2)}
            for d in days[: max(1, min(int(recent_days), 90))]
        ],
        "note": ("Counted from when this ledger was first deployed; trades deleted by the 3-day history "
                 "retention before that are not included. Estimates from Dhan's published NSE rate card."),
    }


def trades(db, day: Optional[str] = None, limit: int = 200) -> dict:
    """Per-trade charges straight from the stored ledger rows of one IST day (default today, newest first), with the
    full split and net P&L. Reads only what is stored (after a sync), so it also works for trades whose
    scalp_positions row the retention job has already deleted. Never writes except the usual sync/restate."""
    sync_all(db)
    day = day or ist_today_str()
    rows = (db.query(ScalpChargesLedger).filter(ScalpChargesLedger.day == day)
            .order_by(ScalpChargesLedger.position_id.desc()).limit(max(1, min(int(limit), 1000))).all())
    restate_stored(db, rows)
    out = []
    for r in rows:
        c = _restated(r)
        qty = int(r.quantity or 0)
        gross = r.gross_pnl
        out.append({
            "position_id": r.position_id, "symbol": r.symbol, "day": r.day, "qty": qty,
            "buy_price": round(r.buy_value / qty, 2) if qty else None,
            "sell_price": round(r.sell_value / qty, 2) if qty else None,
            "buy_value": round(r.buy_value or 0.0, 2), "sell_value": round(r.sell_value or 0.0, 2),
            "brokerage": round(c["brokerage"], 2), "stt": round(c["stt"], 2), "exchange": round(c["exchange"], 2),
            "sebi": round(c["sebi"], 2), "gst": round(c["gst"], 2), "stamp": round(c["stamp"], 2),
            "total_charges": round(c["total"], 2),
            "gross_pnl": round(gross, 2) if gross is not None else None,
            "net_pnl": round(gross - c["total"], 2) if gross is not None else None,
        })
    return {"day": day, "count": len(out), "trades": out}
