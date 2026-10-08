"""Cumulative brokerage report for real-trade-service (2026-10-08, group 258).

trade_orders / trade_fills are never purged in this service, so unlike position-stocks-service no separate
ledger table is needed: the report is computed from every filled order since the first one.

Brokerage is charged per EXECUTED ORDER (not per fill): INTRADAY/MIS = lower of CHARGES_BROKERAGE_CAP_RS and
CHARGES_BROKERAGE_PCT % of the order's filled value; CNC delivery = CHARGES_DELIVERY_BROKERAGE_RS (0). A BUY's
product comes from TradeOrder.product_type (NULL on old rows -> CNC, the automated entry path's only product).
SELL orders do not store a product; exit_engine sells in the product the position was bought in, so a SELL takes
the product of the most recent earlier BUY of the same symbol (CNC when none is known). Estimates, not contract
note figures. Pure helpers + one DB reader; never writes.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

import config
import models
from tz_utils import as_aware, ist_today_str

_DELIVERY = ("CNC", "DELIVERY")


def order_brokerage(value: float, product: Optional[str]) -> float:
    """Brokerage of ONE executed order."""
    if not value or value <= 0:
        return 0.0
    if (product or "CNC").upper() in _DELIVERY:
        return float(config.CHARGES_DELIVERY_BROKERAGE_RS)
    return min(value * (config.CHARGES_BROKERAGE_PCT / 100.0), config.CHARGES_BROKERAGE_CAP_RS)


def build_rows(orders: Iterable[dict]) -> list:
    """orders: dicts with id, symbol, side, product_type, value, created_at(aware), day. Returns one row per order
    with its product resolved and brokerage computed. Input order does not matter (sorted by created_at here)."""
    last_buy_product: dict = {}
    out = []
    for o in sorted(orders, key=lambda x: (x["created_at"], x["id"])):
        side = (o.get("side") or "").upper()
        if side == "BUY":
            product = (o.get("product_type") or "CNC").upper()
            last_buy_product[o["symbol"]] = product
        else:
            product = (o.get("product_type") or last_buy_product.get(o["symbol"]) or "CNC").upper()
        out.append({
            "id": o["id"], "symbol": o["symbol"], "side": side, "product": product, "day": o["day"],
            "value": float(o["value"]), "brokerage": order_brokerage(float(o["value"]), product),
        })
    return out


def summarize(rows: list, recent_days: int = 14) -> dict:
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
    return {
        "since": min(by_day) if by_day else None,
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
        db.query(models.TradeFill.order_id, models.TradeFill.qty, models.TradeFill.price)
        .join(models.TradeOrder, models.TradeOrder.id == models.TradeFill.order_id)
        .filter(models.TradeOrder.mode == mode)
        .all()
    )
    value_by_order: dict = defaultdict(float)
    for oid, qty, price in fill_rows:
        value_by_order[oid] += float(qty or 0) * float(price or 0)
    if not value_by_order:
        return summarize([], recent_days)
    orders = []
    ids = list(value_by_order)
    for i in range(0, len(ids), 500):
        for o in db.query(models.TradeOrder).filter(models.TradeOrder.id.in_(ids[i:i + 500])).all():
            created = as_aware(o.created_at)
            if created is None or value_by_order[o.id] <= 0:
                continue
            orders.append({
                "id": o.id, "symbol": o.symbol, "side": o.side, "product_type": o.product_type,
                "value": value_by_order[o.id], "created_at": created, "day": ist_today_str(created),
            })
    return summarize(build_rows(orders), recent_days)
