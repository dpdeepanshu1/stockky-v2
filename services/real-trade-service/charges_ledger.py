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
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

import config
import models
from datetime import date, timedelta

from tz_utils import as_aware, ist_today_str

_DELIVERY = ("CNC", "DELIVERY")

# Same rate card as the dashboard Charges tab (RealAutoTrade.tsx calcCharges).
_STT_INTRA_PCT = 0.025 / 100
_STT_DELIVERY_SELL_PCT = 0.1 / 100
_EXCHANGE_PCT = 0.00345 / 100
_SEBI_PCT = 0.0001 / 100
_GST = 0.18
_STAMP_DELIVERY_PCT = 0.015 / 100
_STAMP_INTRA_PCT = 0.003 / 100
_DP_CHARGE_RS = 13.5                  # per delivery scrip sold per day


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
        stt = 0.0 if is_buy else value * _STT_DELIVERY_SELL_PCT
        stamp = value * _STAMP_DELIVERY_PCT if is_buy else 0.0
    else:
        stt = value * _STT_INTRA_PCT
        stamp = value * _STAMP_INTRA_PCT if is_buy else 0.0
    exchange = value * _EXCHANGE_PCT
    sebi = value * _SEBI_PCT
    gst = (brokerage + exchange) * _GST
    return {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi, "gst": gst, "stamp": stamp}


def build_rows(orders: Iterable[dict]) -> list:
    """orders: dicts with id, symbol, side, product_type, value, created_at(aware), day. Returns one row per order
    with its product resolved and brokerage computed. Input order does not matter (sorted by created_at here)."""
    last_buy_product: dict = {}
    dp_charged: set = set()
    out = []
    for o in sorted(orders, key=lambda x: (x["created_at"], x["id"])):
        side = (o.get("side") or "").upper()
        if side == "BUY":
            product = (o.get("product_type") or "CNC").upper()
            last_buy_product[o["symbol"]] = product
        else:
            product = (o.get("product_type") or last_buy_product.get(o["symbol"]) or "CNC").upper()
        value = float(o["value"])
        c = order_charges(value, side, product)
        dp = 0.0
        if side == "SELL" and product in _DELIVERY and value > 0 and (o["symbol"], o["day"]) not in dp_charged:
            dp_charged.add((o["symbol"], o["day"]))   # DP billed once per scrip per day
            dp = _DP_CHARGE_RS
        c["dp"] = dp
        out.append({
            "id": o["id"], "symbol": o["symbol"], "side": side, "product": product, "day": o["day"],
            "value": value, "brokerage": c["brokerage"], "charges": c, "all_charges": sum(c.values()),
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
    gst = total * 0.18
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
                      "delivery_rs": config.CHARGES_DELIVERY_BROKERAGE_RS},
        "note": "Estimated from filled orders with the Charges-tab rate card; check against a Dhan contract note.",
    }


def report(db, mode: str = "REAL", recent_days: int = 14) -> dict:
    """Read every order of `mode` with at least one fill and summarise its brokerage."""
    mode = (mode or "REAL").upper()
    fill_rows = (
        db.query(models.TradeFill.order_id, models.TradeFill.qty, models.TradeFill.price, models.TradeFill.filled_at)
        .join(models.TradeOrder, models.TradeOrder.id == models.TradeFill.order_id)
        .filter(models.TradeOrder.mode == mode)
        .all()
    )
    value_by_order: dict = defaultdict(float)
    first_fill: dict = {}
    for oid, qty, price, filled_at in fill_rows:
        value_by_order[oid] += float(qty or 0) * float(price or 0)
        fa = as_aware(filled_at)
        if fa is not None and (oid not in first_fill or fa < first_fill[oid]):
            first_fill[oid] = fa
    if not value_by_order:
        return summarize([], recent_days)
    orders = []
    ids = list(value_by_order)
    for i in range(0, len(ids), 500):
        for o in db.query(models.TradeOrder).filter(models.TradeOrder.id.in_(ids[i:i + 500])).all():
            created = as_aware(o.created_at)
            if created is None or value_by_order[o.id] <= 0:
                continue
            filled = first_fill.get(o.id) or created
            orders.append({
                "id": o.id, "symbol": o.symbol, "side": o.side, "product_type": o.product_type,
                "value": value_by_order[o.id], "created_at": created, "day": ist_today_str(filled),
            })
    return summarize(build_rows(orders), recent_days)
