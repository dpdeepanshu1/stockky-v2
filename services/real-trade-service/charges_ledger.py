"""Cumulative brokerage report for real-trade-service (2026-10-08, group 258).

trade_orders / trade_fills are never purged in this service, so unlike position-stocks-service no separate
ledger table is needed: the report is computed from every filled order since the first one.

Brokerage is charged per EXECUTED ORDER (not per fill): INTRADAY/MIS = lower of CHARGES_BROKERAGE_CAP_RS and
CHARGES_BROKERAGE_PCT % of the order's filled value; CNC delivery = CHARGES_DELIVERY_BROKERAGE_RS (0). A BUY's
product comes from TradeOrder.product_type (NULL on old rows -> CNC, the automated entry path's only product).
SELL orders do not store a product; exit_engine sells in the product the position was bought in, so a SELL takes
the product of the most recent earlier BUY of the same symbol (CNC when none is known). Estimates, not contract
note figures. Pure helpers + one DB reader; never writes.

2026-10-08 (group 259): the report carries ALL Dhan charges (brokerage + STT + exchange + SEBI + GST + stamp + DP,
same rate card as the Charges tab), not just brokerage - delivery brokerage is Rs 0 so a brokerage-only total read
"Rs 0" while today's figure showed ~Rs 93 (mostly the Rs 13.5 DP per delivery sell). Result is laid out as three
rows: first order -> yesterday (a date range), today, grand total. An order's day is its FIRST FILL day (IST).

2026-10-08 (group 262): the rate card is now ONE env-overridable set in config.py (Dhan's published NSE equity card) and
was corrected: delivery STT is 0.1 % on BUY *and* SELL (every CNC buy used to show STT 0), intraday STT is 0.025 % on
the SELL leg only, exchange = 0.00297 % + 0.0001 % IPFT, GST also covers the SEBI fee, DP is Rs 12.50 + GST. DP is not
charged on a sell that only closes shares bought the same day (nothing was debited from demat). A legacy SELL with no
price at all is priced off the last buy of the symbol and flagged `estimated` instead of vanishing (its DP was lost).

2026-10-09 (group 263): every figure is now ALSO STORED (tables trade_charges_ledger = one row per executed order with the
full split incl. DP, trade_pnl_daily = per day gross / charges / net). persist() upserts the rows build_rows() just
computed (restating a stored row in place when the rate card or a repaired fill changes it), persist_daily() refreshes
the day's charges and, for today, the gross from the account row. Both are best-effort and never raise;
sync_throttled() runs them from the fast reconcile tick so storage never waits for a dashboard read.

2026-10-09 (group 266): a CNC buy sold the same day is priced at INTRADAY rates (see build_rows) - verified against the Dhan
contract note of 07-Oct-2026. Before this every same-day CNC round trip was priced as delivery (brokerage 0, STT 0.1 %
on both legs), which overstated STT and missed the 0.03 % brokerage Dhan really charges.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Iterable, Optional

import config
import models
from datetime import date, timedelta

from tz_utils import as_aware, ist_today_str

logger = logging.getLogger("real-trade-charges-ledger")

_DELIVERY = ("CNC", "DELIVERY")

_GST = config.GST_PCT / 100.0


def _pct(x: float) -> float:
    return x / 100.0


def dp_charge_rs() -> float:
    """DP charge for one delivery scrip sold on a day, GST included (Rs 12.50 + 18 % = Rs 14.75)."""
    return config.DP_CHARGE_FLAT * (1.0 + _GST)


def order_brokerage(value: float, product: Optional[str]) -> float:
    """Brokerage of ONE executed order."""
    if not value or value <= 0:
        return 0.0
    if (product or "CNC").upper() in _DELIVERY:
        return float(config.CHARGES_DELIVERY_BROKERAGE_RS)
    return min(value * (config.CHARGES_BROKERAGE_PCT / 100.0), config.CHARGES_BROKERAGE_CAP_RS)


def order_charges(value: float, side: str, product: Optional[str]) -> dict:
    """Every Dhan charge of ONE executed order EXCEPT DP (per scrip per day, see build_rows)."""
    if not value or value <= 0:
        return {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "gst": 0.0, "stamp": 0.0}
    delivery = (product or "CNC").upper() in _DELIVERY
    is_buy = (side or "").upper() == "BUY"
    brokerage = order_brokerage(value, product)
    if delivery:
        stt = value * _pct(config.STT_DELIVERY_PCT_PER_LEG)                  # both legs
        stamp = value * _pct(config.STAMP_DUTY_BUY_PCT_DELIVERY) if is_buy else 0.0
    else:
        stt = 0.0 if is_buy else value * _pct(config.STT_INTRADAY_SELL_PCT)  # sell leg only
        stamp = value * _pct(config.STAMP_DUTY_BUY_PCT_INTRADAY) if is_buy else 0.0
    exchange = value * _pct(config.EXCHANGE_TXN_PCT + config.IPFT_PCT)
    sebi = value * _pct(config.SEBI_TURNOVER_PCT)
    gst = (brokerage + exchange + sebi) * _GST                               # not on STT / stamp duty
    return {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi, "gst": gst, "stamp": stamp}


def _blend(value: float, side: str, intraday_frac: float) -> dict:
    """Charges of one CNC order of which `intraday_frac` (0..1) of the quantity was squared off the same day (Dhan charges
    that part at INTRADAY rates: 0.03 % brokerage, STT 0.025 % on the sell, intraday stamp) and the rest as delivery."""
    f = max(0.0, min(1.0, intraday_frac))
    if f <= 0.0:
        return order_charges(value, side, "CNC")
    if f >= 1.0:
        return order_charges(value, side, "INTRADAY")
    a = order_charges(value, side, "INTRADAY")
    b = order_charges(value, side, "CNC")
    return {k: f * a[k] + (1.0 - f) * b[k] for k in a}


def build_rows(orders: Iterable[dict]) -> list:
    """orders: dicts with id, symbol, side, product_type, value, created_at(aware), day (+ optional qty).
    Returns one row per order with its product resolved and charges computed. Input order does not matter (sorted
    by created_at here).

    group 266 (2026-10-09, verified on Dhan contract note 07-Oct-2026): a CNC BUY that is SOLD THE SAME DAY is charged by
    Dhan as an INTRADAY round trip - brokerage 0.03 % on both legs, STT 0.025 % on the sell only, intraday stamp duty -
    not as delivery (the note's brokerage of Rs 19.93 and STT of Rs 17.00 only reconcile that way). So the shares of a
    delivery BUY that a later SELL of the same symbol closes the same day (FIFO) are priced at intraday rates on BOTH
    orders; any remainder is priced as delivery. A SELL of shares bought on an earlier day stays a delivery sale.

    DP (delivery SELL only, once per scrip per day): skipped when the sell only closes shares BOUGHT THE SAME DAY (they
    never reached demat, so nothing is debited). With no qty known the DP is charged, as before.
    An executed SELL whose value is unknown (value 0 but qty known - a legacy market sell with neither a fill row nor
    a broker notional) is priced at the last earlier BUY's unit price of that symbol and flagged ``estimated``.
    With no qty known the order is priced as a plain delivery order (the old behaviour)."""
    last_buy_product: dict = {}
    last_buy_unit: dict = {}
    # (symbol, day) -> FIFO list of [row_index, unmatched delivery qty] of BUY orders not yet closed by a same-day sell
    open_buys: dict = defaultdict(list)
    dp_charged: set = set()
    pend: list = []                 # one dict per priced order, charges filled in the second pass
    for o in sorted(orders, key=lambda x: (x["created_at"], x["id"])):
        side = (o.get("side") or "").upper()
        qty = float(o.get("qty") or 0)
        value = float(o.get("value") or 0)
        estimated = False
        key = (o["symbol"], o["day"])
        if side == "BUY":
            product = (o.get("product_type") or "CNC").upper()
            last_buy_product[o["symbol"]] = product
            if value > 0 and qty > 0:
                last_buy_unit[o["symbol"]] = value / qty
        else:
            product = (o.get("product_type") or last_buy_product.get(o["symbol"]) or "CNC").upper()
            if value <= 0 and qty > 0 and last_buy_unit.get(o["symbol"]):
                value = qty * last_buy_unit[o["symbol"]]
                estimated = True
        if value <= 0:
            continue
        row = {"o": o, "side": side, "product": product, "value": value, "qty": qty, "key": key,
               "estimated": estimated, "matched": 0.0, "dp": 0.0}
        idx = len(pend)
        pend.append(row)
        if side == "BUY" and product in _DELIVERY and qty > 0:
            open_buys[key].append([idx, qty])
        if side == "SELL" and product in _DELIVERY:
            from_demat = qty
            if qty > 0:
                need = qty
                for slot in open_buys[key]:                      # FIFO: match against same-day delivery buys
                    if need <= 0:
                        break
                    take = min(need, slot[1])
                    if take > 0:
                        slot[1] -= take
                        pend[slot[0]]["matched"] += take
                        row["matched"] += take
                        need -= take
                from_demat = qty - row["matched"]
            else:
                from_demat = 1.0                                   # qty unknown: assume it left demat
            if from_demat > 0 and key not in dp_charged:
                dp_charged.add(key)                                # DP billed once per scrip per day
                row["dp"] = dp_charge_rs()
    out = []
    for r in pend:
        o = r["o"]
        if r["product"] in _DELIVERY:
            frac = (r["matched"] / r["qty"]) if r["qty"] > 0 else 0.0
            c = _blend(r["value"], r["side"], frac)
        else:
            frac = 1.0
            c = order_charges(r["value"], r["side"], r["product"])
        c["dp"] = r["dp"]
        out.append({
            "id": o["id"], "symbol": o["symbol"], "side": r["side"], "product": r["product"], "day": o["day"],
            "value": r["value"], "qty": r["qty"], "brokerage": c["brokerage"], "charges": c,
            "all_charges": sum(c.values()), "estimated": r["estimated"],
            "same_day_frac": round(frac, 4),
        })
    return out


def _period(rows: list, start, end) -> dict:
    """Totals over rows whose day is within [start, end] (inclusive YYYY-MM-DD; None = open ended)."""
    sel = [r for r in rows if (start is None or r["day"] >= start) and (end is None or r["day"] <= end)]
    comp = {k: 0.0 for k in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp", "dp")}
    for r in sel:
        for k in comp:
            comp[k] += r["charges"].get(k, 0.0)
    return {
        "from": start, "to": end, "orders": len(sel),
        "brokerage": round(comp["brokerage"], 2), "brokerage_incl_gst": round(comp["brokerage"] * (1 + _GST), 2),
        "all_charges": round(sum(comp.values()), 2),
        "components": {k: round(v, 2) for k, v in comp.items()},
    }


def summarize(rows: list, recent_days: int = 14, today: Optional[str] = None) -> dict:
    cap = config.CHARGES_BROKERAGE_CAP_RS
    pct = config.CHARGES_BROKERAGE_PCT / 100.0
    paying = [r for r in rows if r["brokerage"] > 0]
    by_day: dict = defaultdict(lambda: {"orders": 0, "brokerage": 0.0})
    by_symbol: dict = defaultdict(float)
    by_product: dict = defaultdict(lambda: {"orders": 0, "brokerage": 0.0, "value": 0.0})
    for r in rows:
        by_day[r["day"]]["orders"] += 1
        by_day[r["day"]]["brokerage"] += r["brokerage"]
        by_symbol[r["symbol"]] += r["brokerage"]
        p = by_product[r["product"]]
        p["orders"] += 1
        p["brokerage"] += r["brokerage"]
        p["value"] += r["value"]
    total = sum(r["brokerage"] for r in rows)
    gst = total * _GST
    # Orders big enough that the flat cap (not the percentage) applies: value * pct >= cap.
    capped = [r for r in paying if pct > 0 and r["value"] * pct >= cap]
    days = sorted(by_day, reverse=True)
    top = sorted(by_symbol.items(), key=lambda kv: kv[1], reverse=True)[:5]
    since = min(by_day) if by_day else None
    today = today or ist_today_str()
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    total_p = _period(rows, None, None)
    total_p["from"], total_p["to"] = since, today
    return {
        "since": since,
        "today_date": today,
        "history": _period(rows, since, yesterday) if since and since <= yesterday else None,
        "today": _period(rows, today, today),
        "total": total_p,
        "all_charges_total": round(sum(r["all_charges"] for r in rows), 2),
        "trading_days": len(by_day),
        "orders": len(rows),
        "orders_paying_brokerage": len(paying),
        "orders_estimated": sum(1 for r in rows if r.get("estimated")),
        "orders_at_cap": len(capped),
        "brokerage_total": round(total, 2),
        "brokerage_incl_gst": round(total + gst, 2),
        "avg_brokerage_per_paying_order": round(total / len(paying), 2) if paying else None,
        "by_product": {k: {"orders": v["orders"], "brokerage": round(v["brokerage"], 2), "value": round(v["value"], 2)}
                       for k, v in by_product.items()},
        "top_symbols": [{"symbol": s, "brokerage": round(b, 2)} for s, b in top if b > 0],
        "recent_days": [{"day": d, "orders": by_day[d]["orders"], "brokerage": round(by_day[d]["brokerage"], 2)}
                        for d in days[: max(1, min(int(recent_days), 90))]],
        "rate_card": {"intraday_pct": config.CHARGES_BROKERAGE_PCT, "cap_rs": cap,
                      "delivery_rs": config.CHARGES_DELIVERY_BROKERAGE_RS,
                      "stt_delivery_pct_per_leg": config.STT_DELIVERY_PCT_PER_LEG,
                      "stt_intraday_sell_pct": config.STT_INTRADAY_SELL_PCT,
                      "exchange_pct": round(config.EXCHANGE_TXN_PCT + config.IPFT_PCT, 6),
                      "sebi_pct": config.SEBI_TURNOVER_PCT, "gst_pct": config.GST_PCT,
                      "stamp_delivery_pct": config.STAMP_DUTY_BUY_PCT_DELIVERY,
                      "stamp_intraday_pct": config.STAMP_DUTY_BUY_PCT_INTRADAY,
                      "dp_rs_incl_gst": round(dp_charge_rs(), 2)},
        "note": "Estimated from filled orders with Dhan's published NSE rate card (a contract note rounds STT and stamp duty to the rupee); check against a Dhan contract note.",
    }


def report(db, mode: str = "REAL", recent_days: int = 14, persist: bool = False, gross_today=None) -> dict:
    """Read every order of `mode` with at least one fill and summarise its charges.

    2026-10-08 (group 260): orders that have NO trade_fills row but did execute (every SELL booked before
    group 260 - reconcile never wrote fills for exits) are priced from what the order row itself kept:
    broker_fill_notional (broker's cumulative filled value) first, else filled_qty_so_far x limit_price
    (an estimate; market sells have no limit price and are skipped), and dated by updated_at.

    group 263: with ``persist=True`` the computed rows are also written to trade_charges_ledger / trade_pnl_daily
    (``gross_today`` = TradeAccount.realized_pnl_today for the day's gross); the default stays read-only.
    """
    mode = (mode or "REAL").upper()
    fill_rows = (
        db.query(models.TradeFill.order_id, models.TradeFill.qty, models.TradeFill.price, models.TradeFill.filled_at)
        .join(models.TradeOrder, models.TradeOrder.id == models.TradeFill.order_id)
        .filter(models.TradeOrder.mode == mode)
        .all()
    )
    value_by_order: dict = defaultdict(float)
    qty_by_order: dict = defaultdict(float)
    first_fill: dict = {}
    for oid, qty, price, filled_at in fill_rows:
        value_by_order[oid] += float(qty or 0) * float(price or 0)
        qty_by_order[oid] += float(qty or 0)
        fa = as_aware(filled_at)
        if fa is not None and (oid not in first_fill or fa < first_fill[oid]):
            first_fill[oid] = fa
    legacy: dict = {}
    for o in (
        db.query(models.TradeOrder)
        .filter(models.TradeOrder.mode == mode, models.TradeOrder.filled_qty_so_far > 0)
        .all()
    ):
        if o.id in value_by_order:
            continue
        v = float(o.broker_fill_notional or 0)
        if v <= 0 and o.limit_price:
            v = float(o.filled_qty_so_far) * float(o.limit_price)
        legacy[o.id] = o                       # value may still be 0: build_rows prices a SELL off the last BUY
        value_by_order[o.id] = max(v, 0.0)
        qty_by_order[o.id] = float(o.filled_qty_so_far)
    if not value_by_order:
        if persist and gross_today is not None:
            persist_daily(db, mode, [], gross_today)
        return summarize([], recent_days)
    rows = _rows_for(db, mode, value_by_order, qty_by_order, first_fill, legacy)
    if persist:
        persist_rows(db, mode, rows)          # group 263: store what was just computed (never raises)
        persist_daily(db, mode, rows, gross_today)
    return summarize(rows, recent_days)


def _rows_for(db, mode, value_by_order, qty_by_order, first_fill, legacy) -> list:
    orders = []
    ids = list(value_by_order)
    for i in range(0, len(ids), 500):
        for o in db.query(models.TradeOrder).filter(models.TradeOrder.id.in_(ids[i:i + 500])).all():
            created = as_aware(o.created_at)
            if created is None or (value_by_order[o.id] <= 0 and o.id not in legacy):
                continue
            filled = first_fill.get(o.id) or (as_aware(o.updated_at) if o.id in legacy else None) or created
            orders.append({
                "id": o.id, "symbol": o.symbol, "side": o.side, "product_type": o.product_type,
                "value": value_by_order[o.id], "qty": qty_by_order.get(o.id, 0.0),
                "created_at": created, "day": ist_today_str(filled),
            })
    return build_rows(orders)


# ── Storage (group 263) ──────────────────────────────────────────────────────────────────────────────────────────
_SPLIT = ("brokerage", "stt", "exchange", "sebi", "gst", "stamp", "dp")


def persist_rows(db, mode: str, rows: list) -> int:
    """Upsert one trade_charges_ledger row per build_rows() row. A stored row whose figures differ from the freshly
    computed ones (rate card changed, a fill was repaired, DP re-attributed) is rewritten; identical rows are left
    alone. Returns the number of rows inserted or changed. Never raises."""
    if not rows:
        return 0
    try:
        mode = (mode or "REAL").upper()
        ids = [r["id"] for r in rows]
        existing: dict = {}
        for i in range(0, len(ids), 500):
            for row in db.query(models.TradeChargesLedger).filter(models.TradeChargesLedger.order_id.in_(ids[i:i + 500])).all():
                existing[row.order_id] = row
        n = 0
        for r in rows:
            c = r["charges"]
            vals = {k: round(float(c.get(k, 0.0)), 4) for k in _SPLIT}
            total = round(float(r["all_charges"]), 4)
            row = existing.get(r["id"])
            if row is None:
                db.add(models.TradeChargesLedger(
                    order_id=r["id"], mode=mode, symbol=r["symbol"], side=r["side"], product=r["product"], day=r["day"],
                    qty=float(r.get("qty") or 0.0), order_value=round(float(r["value"]), 4), total_charges=total,
                    estimated=bool(r.get("estimated")), **vals,
                ))
                n += 1
                continue
            stale = (
                row.day != r["day"] or row.side != r["side"] or (row.product or "") != (r["product"] or "")
                or bool(row.estimated) != bool(r.get("estimated"))
                or abs((row.order_value or 0.0) - r["value"]) > 0.005
                or abs((row.qty or 0.0) - float(r.get("qty") or 0.0)) > 1e-9
                or abs((row.total_charges or 0.0) - total) > 0.0005
                or any(abs((getattr(row, k) or 0.0) - vals[k]) > 0.0005 for k in _SPLIT)
            )
            if stale:
                row.day, row.side, row.product = r["day"], r["side"], r["product"]
                row.qty, row.order_value, row.total_charges = float(r.get("qty") or 0.0), round(float(r["value"]), 4), total
                row.estimated = bool(r.get("estimated"))
                for k in _SPLIT:
                    setattr(row, k, vals[k])
                row.updated_at = models._now()
                n += 1
        if n:
            db.commit()
        return n
    except Exception:
        logger.exception("charges ledger: storing rows failed; totals are still computed on read")
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _upsert_daily(db, mode: str, day: str, *, gross=None, charges=None, orders=None) -> None:
    row = db.query(models.TradePnlDaily).filter(
        models.TradePnlDaily.mode == mode, models.TradePnlDaily.day == day).first()
    if row is None:
        row = models.TradePnlDaily(mode=mode, day=day, charges=0.0, orders=0)
        db.add(row)
    if charges is not None:
        row.charges = round(float(charges), 2)
    if orders is not None:
        row.orders = int(orders)
    if gross is not None:
        row.realized_gross = round(float(gross), 2)
    if row.realized_gross is not None:
        row.net_realized = round(row.realized_gross - (row.charges or 0.0), 2)
    row.updated_at = models._now()


def persist_daily(db, mode: str, rows: list, gross_today=None, today: Optional[str] = None) -> int:
    """Refresh trade_pnl_daily: charges + order count for EVERY day present in `rows`, and today's gross P&L (from
    the account row, when given). Past days keep the gross frozen at their rollover. Never raises."""
    try:
        mode = (mode or "REAL").upper()
        today = today or ist_today_str()
        per_day: dict = defaultdict(lambda: [0.0, 0])
        for r in rows or []:
            per_day[r["day"]][0] += r["all_charges"]
            per_day[r["day"]][1] += 1
        for d, (ch, n) in per_day.items():
            _upsert_daily(db, mode, d, charges=ch, orders=n, gross=(gross_today if d == today else None))
        if gross_today is not None and today not in per_day:
            _upsert_daily(db, mode, today, gross=gross_today)
        db.commit()
        return len(per_day)
    except Exception:
        logger.exception("charges ledger: storing daily P&L failed")
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def freeze_day_gross(db, mode: str, day: Optional[str], gross: float) -> None:
    """Called by portfolio._maybe_reset_daily_pnl just BEFORE realized_pnl_today is zeroed: store that day's final
    gross P&L. Never raises, never leaves the session in a failed state."""
    if not day:
        return
    try:
        _upsert_daily(db, (mode or "REAL").upper(), day, gross=gross)
        db.commit()
    except Exception:
        logger.exception("charges ledger: could not freeze %s gross P&L for %s", mode, day)
        try:
            db.rollback()
        except Exception:
            pass


_last_sync_at: dict = {}


def sync_throttled(db, mode: str = "REAL", min_interval: float = 60.0) -> int:
    """Compute + store charges at most once per `min_interval` s per mode (called from the fast reconcile tick).
    Returns the number of executed orders processed, 0 when throttled or on failure. Never raises."""
    mode = (mode or "REAL").upper()
    now = time.monotonic()
    last = _last_sync_at.get(mode)
    if last is not None and now - last < min_interval:
        return 0
    _last_sync_at[mode] = now
    try:
        gross_today = None
        try:
            acct = db.query(models.TradeAccount).filter_by(mode=mode).first()
            if acct is not None and acct.pnl_last_reset_date == ist_today_str():
                gross_today = float(acct.realized_pnl_today or 0.0)
        except Exception:
            gross_today = None
        rep = report(db, mode, 14, persist=True, gross_today=gross_today)
        return int(rep.get("orders") or 0)
    except Exception:
        logger.exception("charges ledger: sync failed")
        return 0


def daily_history(db, mode: str = "REAL", days: int = 30) -> list:
    """Stored per-day gross / charges / net, newest first (read-only)."""
    rows = (db.query(models.TradePnlDaily).filter(models.TradePnlDaily.mode == (mode or "REAL").upper())
            .order_by(models.TradePnlDaily.day.desc()).limit(max(1, min(int(days), 365))).all())
    return [{"day": r.day, "orders": r.orders, "realized_gross": r.realized_gross, "charges": round(r.charges or 0.0, 2),
             "net_realized": r.net_realized} for r in rows]
