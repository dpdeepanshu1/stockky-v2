"""A13: hourly broker-vs-book drift check (REAL mode, read-only).

The per-cycle reconcile (execution/reconcile.py + portfolio.holdings_sync_reconcile) already force-closes a position the
broker does not hold and caps qty down when the broker holds less. It never looks at the other direction, never reports
what it could not fix (a PENDING_EXIT position, an order stuck PLACED for hours), and says nothing when all is well.
This pass changes NOTHING in the book. Once an hour it compares the book with the broker's holdings + positions and the
order table, keeps the last report for GET /reconcile-report/{mode}, and sends one Telegram message only when the drift
set changed (or every 4 h while it persists), plus one "clean again" message.

Drift kinds
  warn  missing_at_broker   book holds qty, broker holds none (older than HOLDINGS_SYNC_GUARD_MINUTES)
  warn  book_over_broker    broker holds fewer shares than the book
  warn  stale_order         REAL order still PLACED / PARTIAL after HOURLY_RECONCILE_STALE_ORDER_MINUTES
  info  broker_over_book    broker holds more than the book (the account is shared with position-stocks, so this alone is not an alarm)
  info  broker_only         broker symbols with no open book row (same reason)

Fail-open: a broker or DB error gives a report with `error` set and never raises into the tick.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("real-trade-hourly-reconcile")

WARN_KINDS = ("missing_at_broker", "book_over_broker", "stale_order")
_LIVE_STATUSES = ("OPEN", "PARTIALLY_CLOSED", "PENDING_EXIT")
_REALERT_AFTER = timedelta(hours=4)
EDIS_CAUSE = "cdsl_edis"
_EDIS_FRESH = timedelta(hours=6)         # the exit path re-saves its CDSL marker every EXIT_CDSL_ALERT_COOLDOWN_MIN (60) while the block lasts

_last_run_at: Optional[datetime] = None
_last_report: Optional[dict] = None
_last_alert_sig: Optional[str] = None
_last_alert_at: Optional[datetime] = None


def _aware(ts):
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _get(row: dict, *keys, default=None):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return default


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def broker_qty_map(holdings, positions) -> dict:
    """symbol -> shares the broker reports (largest of demat holding qty and live position qty). Pure."""
    out: dict = {}
    for row in holdings or []:
        if not isinstance(row, dict):
            continue
        sym = _get(row, "tradingSymbol", "trading_symbol", "symbol")
        qty = _int(_get(row, "totalQty", "total_qty", "quantity", default=0))
        if sym and qty > 0:
            k = str(sym).upper().strip()
            out[k] = max(out.get(k, 0), qty)
    for row in positions or []:
        if not isinstance(row, dict):
            continue
        sym = _get(row, "tradingSymbol", "trading_symbol", "symbol")
        qty = max(_int(_get(row, "netQty", "net_qty", default=0)),
                  _int(_get(row, "positiveQty", "positive_qty", "buyQty", "buy_qty", default=0)))
        if sym and qty > 0:
            k = str(sym).upper().strip()
            out[k] = max(out.get(k, 0), qty)
    return out


def compute_drift(book: list, broker_qty: dict, stale_orders: list) -> list:
    """Pure. `book` = [{symbol, qty, status, young}] (young = inside the settlement-lag guard, skipped for broker-missing / short
    checks); `stale_orders` = [{id, symbol, side, status, age_min}]. Returns a list of {kind, severity, symbol, ...}."""
    drift: list = []
    book_by_sym: dict = {}
    for r in book:
        sym = str(r["symbol"]).upper().strip()
        book_by_sym[sym] = book_by_sym.get(sym, 0) + int(r["qty"])
    young = {str(r["symbol"]).upper().strip() for r in book if r.get("young")}
    for sym, bqty in sorted(book_by_sym.items()):
        if bqty <= 0 or sym in young:
            continue
        got = int(broker_qty.get(sym, 0))
        if got <= 0:
            drift.append({"kind": "missing_at_broker", "severity": "warn", "symbol": sym, "book_qty": bqty, "broker_qty": 0})
        elif got < bqty:
            drift.append({"kind": "book_over_broker", "severity": "warn", "symbol": sym, "book_qty": bqty, "broker_qty": got})
        elif got > bqty:
            drift.append({"kind": "broker_over_book", "severity": "info", "symbol": sym, "book_qty": bqty, "broker_qty": got})
    only = sorted(s for s in broker_qty if s not in book_by_sym)
    if only:
        drift.append({"kind": "broker_only", "severity": "info", "symbols": only, "count": len(only)})
    for o in stale_orders:
        item = {"kind": "stale_order", "severity": "warn", "symbol": o["symbol"], "order_id": o["id"],
                "side": o["side"], "status": o["status"], "age_minutes": o["age_min"]}
        if o.get("cause"):
            item["cause"] = o["cause"]
        drift.append(item)
    return drift


def signature(drift: list) -> str:
    """Stable id of the WARN-level drift set (what an alert is about)."""
    parts = []
    for d in drift:
        if d.get("severity") != "warn":
            continue
        parts.append(f"{d['kind']}:{d.get('symbol')}:{d.get('book_qty', '')}:{d.get('broker_qty', '')}:{d.get('order_id', '')}"
                     + (f":{d['cause']}" if d.get("cause") else ""))
    return "|".join(sorted(parts))


def alert_action(sig: str, last_sig: Optional[str], last_at: Optional[datetime], now: datetime) -> Optional[str]:
    """Pure. 'alert' when there is warn-level drift and it is new or has lasted 4 h since the last alert; 'clear' once after drift
    that was alerted goes away; None otherwise."""
    if sig:
        if sig != last_sig or last_at is None or (now - last_at) >= _REALERT_AFTER:
            return "alert"
        return None
    return "clear" if last_sig else None


def due(now: Optional[datetime] = None) -> bool:
    import config
    mins = int(getattr(config, "HOURLY_RECONCILE_INTERVAL_MINUTES", 60) or 0)
    if mins <= 0:
        return False
    if _last_run_at is None:
        return True
    return ((now or datetime.now(timezone.utc)) - _last_run_at) >= timedelta(minutes=mins)


def edis_blocked(db, symbol: str, now: datetime, verified: Optional[bool] = None) -> bool:
    """True when the exit path recorded a CDSL eDIS/TPIN refusal for an open REAL position in `symbol` in the last 6 h (the same
    `cdsl_alert_last_<position id>` marker exit.py writes when Dhan answers "Validate Qty from CDSL") AND Dhan does not currently report
    the holdings as verified. `verified` = edis_verification_summary()["verified_today"]; True means the TPIN step is done, so an old marker
    (it outlives the fix by up to 6 h) must not be blamed for a stale order. Never raises."""
    if verified is True:
        return False
    try:
        import models
        from resilience.local_cache import load_snapshot
        for p in db.query(models.TradePosition).filter(
                models.TradePosition.mode == "REAL", models.TradePosition.symbol == symbol,
                models.TradePosition.status.in_(_LIVE_STATUSES)).all():
            at = (load_snapshot(db, f"cdsl_alert_last_{p.id}") or {}).get("at")
            if at:
                ts = _aware(datetime.fromisoformat(at))
                if ts is not None and (now - ts) <= _EDIS_FRESH:
                    return True
    except Exception:  # noqa: BLE001
        logger.debug("hourly reconcile: eDIS marker lookup failed (ignored)", exc_info=True)
    return False


def last_report() -> Optional[dict]:
    return _last_report


def _format_alert(drift: list) -> str:
    lines = ["🔎 *Hourly broker reconcile — REAL* — book and broker disagree (nothing was changed):"]
    for d in drift:
        if d.get("severity") != "warn":
            continue
        k = d["kind"]
        if k == "missing_at_broker":
            lines.append(f"• {d['symbol']}: book {d['book_qty']}, broker 0")
        elif k == "book_over_broker":
            lines.append(f"• {d['symbol']}: book {d['book_qty']}, broker {d['broker_qty']}")
        elif k == "stale_order":
            lines.append(f"• order #{d['order_id']} {d['symbol']} {d['side']} still {d['status']} after {d['age_minutes']} min")
            if d.get("cause") == EDIS_CAUSE:
                lines.append(f"   ↳ cause: Dhan refuses to sell {d['symbol']} until CDSL eDIS/TPIN is authorised. "
                             "Open the Dhan app -> Verify Holdings and enter the TPIN; the exit retries on its own afterwards.")
    lines.append("Check Orders / Positions; the per-cycle sync handles the first two kinds on its own once past the guard window.")
    return "\n".join(lines)


async def run_hourly_reconcile(db, notify=None, now: Optional[datetime] = None) -> dict:
    """One read-only pass. Never raises. `notify` is an async callable(text); defaults to notifier.notify_async."""
    global _last_run_at, _last_report, _last_alert_sig, _last_alert_at
    now = now or datetime.now(timezone.utc)
    _last_run_at = now
    report: dict = {"ran_at": now.isoformat(), "mode": "REAL", "drift": [], "warn_count": 0, "error": None}
    try:
        import config
        import models
        from execution import dhan_client
        holdings = dhan_client.get_holdings(db)
        positions = dhan_client.get_positions(db)
        guard = now - timedelta(minutes=int(getattr(config, "HOLDINGS_SYNC_GUARD_MINUTES", 30)))
        rows = db.query(models.TradePosition).filter(
            models.TradePosition.mode == "REAL", models.TradePosition.status.in_(_LIVE_STATUSES),
            models.TradePosition.qty_open > 0).all()
        book = [{"symbol": p.symbol, "qty": int(p.qty_open), "status": p.status,
                 "young": (_aware(p.opened_at) or now) > guard} for p in rows]
        stale_min = int(getattr(config, "HOURLY_RECONCILE_STALE_ORDER_MINUTES", 90))
        cutoff = now - timedelta(minutes=stale_min)
        stale = []
        _ver: dict = {}

        def _verified():
            """Dhan's own answer to "is today's CDSL authorisation done?", fetched at most once per pass and only when a stale SELL needs it."""
            if "v" not in _ver:
                try:
                    _ver["v"] = dhan_client.edis_verification_summary(db).get("verified_today")
                except Exception:  # noqa: BLE001
                    _ver["v"] = None
            return _ver["v"]
        for o in db.query(models.TradeOrder).filter(
                models.TradeOrder.mode == "REAL", models.TradeOrder.status.in_(("PLACED", "PARTIAL"))).all():
            ts = _aware(o.updated_at or o.created_at)
            if ts is not None and ts < cutoff:
                item = {"id": o.id, "symbol": o.symbol, "side": o.side, "status": o.status,
                        "age_min": int((now - ts).total_seconds() // 60)}
                if str(o.side).upper() == "SELL" and edis_blocked(db, o.symbol, now, _verified()):
                    item["cause"] = EDIS_CAUSE
                stale.append(item)
        drift = compute_drift(book, broker_qty_map(holdings, positions), stale)
        report["drift"] = drift
        report["warn_count"] = sum(1 for d in drift if d.get("severity") == "warn")
        report["book_positions"] = len(book)
    except Exception as e:  # noqa: BLE001 - must never break the tick
        logger.warning("hourly reconcile failed (non-fatal): %s: %s", type(e).__name__, e)
        report["error"] = f"{type(e).__name__}: {e}"
        _last_report = report
        return report

    sig = signature(report["drift"])
    if notify is None:
        from notifier import notify_async as notify
    try:
        action = alert_action(sig, _last_alert_sig, _last_alert_at, now)
        if action == "alert":
            await notify(_format_alert(report["drift"]))
            _last_alert_sig, _last_alert_at = sig, now
            report["alerted"] = True
        elif action == "clear":
            await notify("✅ *Hourly broker reconcile — REAL* — book and broker agree again.")
            _last_alert_sig, _last_alert_at = None, None
            report["alerted"] = True
    except Exception as e:  # noqa: BLE001
        logger.warning("hourly reconcile alert failed (non-fatal): %s", e)
    logger.info("hourly reconcile: %d book position(s), %d warning(s), %d info", report.get("book_positions", 0),
                report["warn_count"], len(report["drift"]) - report["warn_count"])
    _last_report = report
    return report
