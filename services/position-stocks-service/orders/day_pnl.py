"""orders/day_pnl.py - today's realised P&L straight from Dhan's order book (group348).

WHY: on 13 Oct the app showed about -Rs 25 for the day while Dhan's own per-symbol totals summed to about -Rs 73. The difference was
double exits (a stop leg and our market exit both selling, leaving a short that had to be bought back) and trades placed by another
service. The app's P&L only follows positions this service booked; this one follows the broker, so nothing can hide.

WHAT: pure function summarize(orders) over Dhan's order list. Per symbol: bought / sold quantity and average prices, gross P&L on the
matched quantity, and net_qty (bought - sold). A symbol with net_qty != 0 is not flat: negative = an accidental short (double sell),
positive = still held. Gross of charges. Covers every order on the account, from every service. Never raises on odd rows."""
from __future__ import annotations

from typing import Iterable, Optional

_FILLED = ("TRADED", "FILLED", "EXECUTED", "COMPLETE", "PART_TRADED", "PARTIALLY_TRADED")


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def summarize(orders: Optional[Iterable[dict]]) -> dict:
    per: dict = {}
    skipped = 0
    for r in orders or []:
        try:
            if str(r.get("orderStatus") or r.get("order_status") or "").upper() not in _FILLED:
                continue
            side = str(r.get("transactionType") or r.get("transaction_type") or "").upper()
            qty = _num(r.get("filledQty") or r.get("tradedQuantity") or r.get("quantity"))
            px = _num(r.get("averageTradedPrice") or r.get("average_traded_price") or r.get("price"))
            sym = str(r.get("tradingSymbol") or r.get("trading_symbol") or r.get("securityId") or r.get("security_id") or "").strip()
            if side not in ("BUY", "SELL") or not qty or qty <= 0 or not px or px <= 0 or not sym:
                skipped += 1
                continue
            d = per.setdefault(sym, {"bq": 0.0, "bv": 0.0, "sq": 0.0, "sv": 0.0})
            if side == "BUY":
                d["bq"] += qty
                d["bv"] += qty * px
            else:
                d["sq"] += qty
                d["sv"] += qty * px
        except Exception:  # noqa: BLE001 - one odd row must not hide the rest
            skipped += 1
    rows, total = [], 0.0
    for sym, d in sorted(per.items()):
        matched = min(d["bq"], d["sq"])
        avg_b = d["bv"] / d["bq"] if d["bq"] else None
        avg_s = d["sv"] / d["sq"] if d["sq"] else None
        pnl = matched * (avg_s - avg_b) if matched and avg_b and avg_s else 0.0
        total += pnl
        net = d["bq"] - d["sq"]
        rows.append({"symbol": sym, "bought_qty": int(d["bq"]), "sold_qty": int(d["sq"]),
                     "avg_buy": round(avg_b, 4) if avg_b else None, "avg_sell": round(avg_s, 4) if avg_s else None,
                     "net_qty": int(net), "gross_pnl": round(pnl, 2),
                     "flag": "SHORT" if net < 0 else ("HELD" if net > 0 else "")})
    return {"gross_pnl": round(total, 2), "symbols": len(rows), "not_flat": [x["symbol"] for x in rows if x["net_qty"]],
            "rows": rows, "skipped_rows": skipped, "note": "gross of charges; every order on the Dhan account, all services"}
