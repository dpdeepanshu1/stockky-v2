"""Per-trade record and expectancy report (group 290, plan Phase B: measure before tuning).

One record per CLOSED position, built on read from tables that already exist (no new table, no migration):

  position            avg entry price, realized P&L, opened_at / closed_at, source_tab, watchlist_entry_id
  BUY / SELL orders   matched to the position by mode + symbol + time (see _assign), their fills and the SELL's exit_reason
  decision/candidate  signal_price the entry was based on
  watchlist entry     tier (1 full pipeline, 2 degraded, 3 volume-shock), catalyst type and catalyst price
  charges             trade_charges_ledger rows of the matched orders (actual, or flagged estimated); else the position's
                      own round-trip estimate

Each record answers: which tier / catalyst, how far the fill was from the signal and from the catalyst, what time of day,
how it ended (exit reason), what it cost and what was left. `expectancy_report` groups the records by exit reason, entry
hour, tier, catalyst, source tab and day. Read-only; a record that cannot be built is skipped with a warning, an unknown
field is None (never 0). Broker-imported holdings are left out: the system did not enter them.
Expectancy = average P&L per trade (net of charges where known, else gross). Groups under `min_trades_note` trades are noise.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from tz_utils import as_aware, ist_now

logger = logging.getLogger("real-trade-records")

_EPS = 0.005
_ORDER_SLACK_BEFORE_S = 120        # an entry order's fill may be booked slightly before opened_at
_ORDER_SLACK_AFTER_S = 3600        # a late SELL fill still belongs to a position closed up to an hour ago
_FILLED_STATUSES = ("FILLED", "PARTIAL")
_TIER_NAMES = {1: "tier1_full_pipeline", 2: "tier2_degraded", 3: "tier3_volume_shock"}


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _norm_reason(r) -> Optional[str]:
    if r is None:
        return None
    s = "_".join(str(r).strip().lower().split())
    return s or None


def _ist_hhmm(ts) -> Optional[str]:
    return ist_now(as_aware(ts)).strftime("%H:%M") if ts is not None else None


def _bucket(ts) -> Optional[str]:
    if ts is None:
        return None
    t = ist_now(as_aware(ts))
    return f"{t.hour:02d}:{0 if t.minute < 30 else 30:02d}"


def _order_time(order, fills_by_order: dict) -> Optional[datetime]:
    fl = [as_aware(f.filled_at) for f in fills_by_order.get(order.id, []) if getattr(f, "filled_at", None) is not None]
    if fl:
        return min(fl)
    return as_aware(getattr(order, "updated_at", None) or getattr(order, "created_at", None))


def _has_fill(order, fills_by_order: dict) -> bool:
    if any((_num(f.qty) or 0) > 0 for f in fills_by_order.get(order.id, [])):
        return True
    return str(getattr(order, "status", "") or "").upper() in _FILLED_STATUSES


def _vwap(order_ids: list, fills_by_order: dict) -> Optional[float]:
    qty = val = 0.0
    for oid in order_ids:
        for f in fills_by_order.get(oid, []):
            q, p = _num(f.qty), _num(f.price)
            if q and q > 0 and p and p > 0:
                qty += q
                val += q * p
    return round(val / qty, 4) if qty > 0 else None


def _assign(positions: list, orders: list, fills_by_order: dict) -> dict:
    """position.id -> {"buys": [orders], "sells": [orders]} (time ordered). An order belongs to the position of the same mode
    and symbol with the LATEST opened_at that is not more than _ORDER_SLACK_BEFORE_S after the order's time, provided the
    order is not more than _ORDER_SLACK_AFTER_S after that position closed. Orders that never filled are ignored."""
    by_key: dict = defaultdict(list)
    for p in positions:
        by_key[(p.mode, p.symbol)].append(p)
    for lst in by_key.values():
        lst.sort(key=lambda p: as_aware(p.opened_at))
    out = {p.id: {"buys": [], "sells": []} for p in positions}
    timed = []
    for o in orders:
        if (o.mode, o.symbol) not in by_key or not _has_fill(o, fills_by_order):
            continue
        t = _order_time(o, fills_by_order)
        if t is not None:
            timed.append((t, o))
    timed.sort(key=lambda x: x[0])
    for t, o in timed:
        chosen = None
        for p in by_key[(o.mode, o.symbol)]:
            if as_aware(p.opened_at) - timedelta(seconds=_ORDER_SLACK_BEFORE_S) <= t:
                chosen = p
            else:
                break
        if chosen is None:
            continue
        if chosen.closed_at is not None and t > as_aware(chosen.closed_at) + timedelta(seconds=_ORDER_SLACK_AFTER_S):
            continue
        side = str(o.side or "").upper()
        if side == "BUY":
            out[chosen.id]["buys"].append(o)
        elif side == "SELL":
            out[chosen.id]["sells"].append(o)
    return out


def _charges(order_ids: list, ledger_by_order: dict, pos) -> tuple:
    """(charges, source): 'ledger' (actual), 'ledger_estimated' (some row estimated), 'estimate' (the position's own
    round-trip estimate, used when the ledger does not cover EVERY matched order), or (None, None)."""
    if order_ids and all(ledger_by_order.get(oid) for oid in order_ids):
        rows = [r for oid in order_ids for r in ledger_by_order[oid]]
        total = sum(_num(r.total_charges) or 0.0 for r in rows)
        return round(total, 2), ("ledger_estimated" if any(getattr(r, "estimated", False) for r in rows) else "ledger")
    est = _num(getattr(pos, "realized_cost_estimate", None))
    if est is not None and est > 0:
        return round(est, 2), "estimate"
    return None, None


def build_records(positions: Iterable, orders: Iterable, fills: Iterable, decisions: Iterable, candidates: Iterable,
                  watchlist: Iterable, ledger: Iterable) -> list:
    """Pure: one dict per CLOSED, system-entered position that has a realized P&L (see module docstring)."""
    closed = [p for p in positions
              if getattr(p, "status", None) == "CLOSED" and getattr(p, "realized_pnl", None) is not None
              and getattr(p, "opened_at", None) is not None and not getattr(p, "broker_imported", False)]
    orders = list(orders)
    fills_by_order: dict = defaultdict(list)
    for f in fills:
        fills_by_order[f.order_id].append(f)
    decisions_by_id = {d.id: d for d in decisions}
    cand_by_id = {c.id: c for c in candidates}
    watch_by_id = {w.id: w for w in watchlist}
    ledger_by_order: dict = defaultdict(list)
    for r in ledger:
        ledger_by_order[r.order_id].append(r)
    assigned = _assign(closed, orders, fills_by_order)

    out = []
    for p in sorted(closed, key=lambda p: as_aware(p.closed_at or p.opened_at)):
        try:
            a = assigned[p.id]
            buys, sells = a["buys"], a["sells"]
            entry_order = buys[0] if buys else None
            closing_sell = sells[-1] if sells else None
            decision = decisions_by_id.get(getattr(entry_order, "decision_id", None)) if entry_order else None
            cand = cand_by_id.get(getattr(decision, "candidate_id", None)) if decision else None
            w_id = getattr(p, "watchlist_entry_id", None) or (getattr(entry_order, "watchlist_entry_id", None) if entry_order else None)
            w = watch_by_id.get(w_id) if w_id else None

            entry = _num(p.avg_entry_price)
            signal = _num(getattr(cand, "signal_price", None))
            signal = signal if signal and signal > 0 else None
            catalyst = _num(getattr(w, "catalyst_price", None))
            catalyst = catalyst if catalyst and catalyst > 0 else None
            exit_price = _vwap([o.id for o in sells], fills_by_order)
            gross = round(float(p.realized_pnl), 2)
            order_ids = [o.id for o in buys + sells]
            charges, charges_src = _charges(order_ids, ledger_by_order, p)
            if charges_src is not None:
                net = round(gross - charges, 2)
            else:
                nr = _num(getattr(p, "net_realized_pnl", None))
                net = round(nr, 2) if nr is not None else None
                if net is not None:
                    charges_src = "net_realized_pnl"
            reason = None
            if closing_sell is not None:
                reason = _norm_reason(getattr(closing_sell, "exit_reason", None))
                if reason is None and str(getattr(closing_sell, "execution_source", "") or "").upper() == "MANUAL":
                    reason = "manual"
            tier = getattr(w, "source_tier", None)
            held = None
            if p.closed_at is not None:
                held = round((as_aware(p.closed_at) - as_aware(p.opened_at)).total_seconds() / 60.0, 1)
            out.append({
                "position_id": p.id, "mode": p.mode, "symbol": p.symbol,
                "source_tab": getattr(p, "source_tab", None),
                "decision_label": getattr(p, "entry_decision_label", None),
                "conviction": _num(getattr(p, "entry_conviction_score", None)),
                "regime_override": bool(getattr(p, "is_regime_override", False)),
                "tier": tier, "tier_name": _TIER_NAMES.get(tier) if tier is not None else None,
                "catalyst_type": getattr(w, "catalyst_type", None),
                "catalyst_price": catalyst,
                "catalyst_price_source": getattr(w, "catalyst_price_source", None),
                "signal_price": signal,
                "entry_price": entry,
                "entry_slippage_pct": (round((entry - signal) / signal * 100.0, 3)
                                       if entry and signal else None),   # + = paid above the signal
                "entry_vs_catalyst_pct": (round((entry - catalyst) / catalyst * 100.0, 3)
                                          if entry and catalyst else None),
                "exit_price": exit_price,
                "entry_time_ist": _ist_hhmm(p.opened_at), "entry_bucket_ist": _bucket(p.opened_at),
                "entry_hour_ist": (_bucket(p.opened_at) or "")[:2] or None,
                "exit_time_ist": _ist_hhmm(p.closed_at),
                "exit_day_ist": ist_now(as_aware(p.closed_at)).strftime("%Y-%m-%d") if p.closed_at else None,
                "held_minutes": held,
                "exit_reason": reason, "exits": len(sells), "entries": len(buys),
                "gross_pnl": gross, "charges": charges, "charges_source": charges_src, "net_pnl": net,
                "orders_matched": bool(buys or sells),
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("trade record for position %s skipped: %s: %s", getattr(p, "id", "?"), type(e).__name__, e)
    return out


def _pnl_used(r: dict) -> float:
    return r["net_pnl"] if r.get("net_pnl") is not None else r["gross_pnl"]


def _mean(xs: list) -> Optional[float]:
    return round(sum(xs) / len(xs), 2) if xs else None


def _agg(rows: list, min_trades_note: int) -> dict:
    used = [_pnl_used(r) for r in rows]
    wins = [x for x in used if x > _EPS]
    losses = [x for x in used if x < -_EPS]
    avg_win, avg_loss = _mean(wins), _mean(losses)
    nets = [r["net_pnl"] for r in rows if r.get("net_pnl") is not None]
    charges = [r["charges"] for r in rows if r.get("charges") is not None]
    slips = [r["entry_slippage_pct"] for r in rows if r.get("entry_slippage_pct") is not None]
    held = [r["held_minutes"] for r in rows if r.get("held_minutes") is not None]
    return {
        "trades": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(rows), 1),
        "gross_pnl": round(sum(r["gross_pnl"] for r in rows), 2),
        "charges": round(sum(charges), 2) if charges else None,
        "net_pnl": round(sum(nets), 2) if nets else None,
        "net_known_trades": len(nets),
        "expectancy": round(sum(used) / len(used), 2),
        "avg_win": avg_win, "avg_loss": avg_loss,
        "payoff_ratio": round(avg_win / abs(avg_loss), 2) if avg_win is not None and avg_loss else None,
        "avg_entry_slippage_pct": round(sum(slips) / len(slips), 3) if slips else None,
        "avg_held_minutes": round(sum(held) / len(held), 1) if held else None,
        "low_sample": len(rows) < min_trades_note,
    }


def expectancy_report(records: list, *, min_trades_note: int = 5) -> dict:
    """Group `records` (from build_records) and report expectancy per group. Pure."""
    out: dict = {
        "trades": len(records),
        "overall": _agg(records, min_trades_note) if records else None,
        "by_exit_reason": {}, "by_entry_hour_ist": {}, "by_tier": {}, "by_catalyst_type": {},
        "by_source_tab": {}, "by_day": {},
        "note": (f"groups with fewer than {min_trades_note} trades are noise (low_sample); expectancy is the average P&L "
                 "per trade, net of charges where known (ledger actuals, else the position's own estimate), else gross; "
                 "entry_slippage_pct > 0 means the entry was paid above the signal price"),
    }
    if not records:
        return out
    keyers = {
        "by_exit_reason": lambda r: r.get("exit_reason") or "unknown",
        "by_entry_hour_ist": lambda r: r.get("entry_hour_ist") or "?",
        "by_tier": lambda r: (r.get("tier_name") or "no_watchlist_tier"),
        "by_catalyst_type": lambda r: r.get("catalyst_type") or "none",
        "by_source_tab": lambda r: r.get("source_tab") or "?",
        "by_day": lambda r: r.get("exit_day_ist") or "?",
    }
    for name, key in keyers.items():
        groups: dict = defaultdict(list)
        for r in records:
            groups[key(r)].append(r)
        out[name] = {k: _agg(v, min_trades_note) for k, v in sorted(groups.items())}
    return out


def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_records(db, mode: str, days: int = 7, symbol: Optional[str] = None) -> list:
    """Records for CLOSED positions of `mode` closed in the last `days` days (optionally one symbol). Read-only."""
    import models
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    q = db.query(models.TradePosition).filter(
        models.TradePosition.mode == mode, models.TradePosition.status == "CLOSED",
        models.TradePosition.closed_at >= cutoff)
    if symbol:
        q = q.filter(models.TradePosition.symbol == symbol.upper())
    positions = q.all()
    if not positions:
        return []
    syms = sorted({p.symbol for p in positions})
    earliest = min(as_aware(p.opened_at) for p in positions).replace(tzinfo=None) - timedelta(days=1)
    orders: list = []
    for part in _chunks(syms):
        orders += (db.query(models.TradeOrder)
                   .filter(models.TradeOrder.mode == mode, models.TradeOrder.symbol.in_(part),
                           models.TradeOrder.created_at >= earliest).all())
    order_ids = [o.id for o in orders]
    fills, ledger = [], []
    for part in _chunks(order_ids):
        fills += db.query(models.TradeFill).filter(models.TradeFill.order_id.in_(part)).all()
        ledger += db.query(models.TradeChargesLedger).filter(models.TradeChargesLedger.order_id.in_(part)).all()
    decision_ids = sorted({o.decision_id for o in orders if o.decision_id})
    decisions: list = []
    for part in _chunks(decision_ids):
        decisions += db.query(models.TradeDecision).filter(models.TradeDecision.id.in_(part)).all()
    cand_ids = sorted({d.candidate_id for d in decisions if d.candidate_id})
    candidates: list = []
    for part in _chunks(cand_ids):
        candidates += db.query(models.TradeCandidate).filter(models.TradeCandidate.id.in_(part)).all()
    w_ids = sorted({x for x in [p.watchlist_entry_id for p in positions] + [o.watchlist_entry_id for o in orders] if x})
    watchlist: list = []
    for part in _chunks(w_ids):
        watchlist += db.query(models.WatchlistEntry).filter(models.WatchlistEntry.id.in_(part)).all()
    return build_records(positions, orders, fills, decisions, candidates, watchlist, ledger)


# ── daily report: today's expectancy, stored as a snapshot and pushed once after the close (group 292) ─────────────────
SNAPSHOT_PREFIX = "daily_report:"          # key = daily_report:<MODE>:<YYYY-MM-DD> in the existing trade_resilience_cache table


def snapshot_key(mode: str, day: str) -> str:
    return f"{SNAPSHOT_PREFIX}{mode}:{day}"


def records_for_day(records: list, day: str) -> list:
    """Records whose position closed on IST calendar day `day` (YYYY-MM-DD)."""
    return [r for r in records if r.get("exit_day_ist") == day]


def closed_count_for_day(db, mode: str, day: str) -> Optional[int]:
    """group294: how many positions of `mode` are CLOSED with closed_at on IST calendar day `day`. One cheap COUNT, used to
    notice a trade that closed after the report was built. None when it cannot be read (never raises)."""
    try:
        import models
        from tz_utils import IST
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST)
        lo = start.astimezone(timezone.utc).replace(tzinfo=None)
        hi = (start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        return int(db.query(models.TradePosition).filter(
            models.TradePosition.mode == mode, models.TradePosition.status == "CLOSED",
            models.TradePosition.closed_at >= lo, models.TradePosition.closed_at < hi).count())
    except Exception:  # noqa: BLE001
        return None


def build_daily_snapshot(db, mode: str, day: Optional[str] = None, *, week_days: int = 7) -> dict:
    """`day`'s (default: today IST) records and expectancy report plus the rolling `week_days`-day report for context.
    Read-only. Raises only if the database read itself fails (the caller decides whether to retry)."""
    from tz_utils import ist_today_str
    day = day or ist_today_str()
    week_days = max(2, min(int(week_days), 60))
    recs = load_records(db, mode, week_days)
    today = records_for_day(recs, day)
    closed_count = closed_count_for_day(db, mode, day)
    return {
        "mode": mode, "day": day, "generated_at": datetime.now(timezone.utc).isoformat(),
        "today": expectancy_report(today), "week_days": week_days, "week": expectancy_report(recs),
        "records_today": today, "closed_count": closed_count,
    }


def charges_pending(snap: Optional[dict]) -> int:
    """group293: how many of the snapshot's closed trades of the day still carry ESTIMATED (or no) charges, i.e. whose
    orders the charges ledger has not fully booked ('ledger' is the only final source). 0 = the snapshot is final."""
    recs = (snap or {}).get("records_today") or []
    return sum(1 for r in recs if r.get("charges_source") != "ledger")


def snapshot_figures(snap: Optional[dict]) -> tuple:
    """(trades, charges, net_pnl) of a snapshot's day: what a reader of the message would see change."""
    o = ((snap or {}).get("today") or {}).get("overall") or {}
    return (o.get("trades", 0), o.get("charges"), o.get("net_pnl"))


def _money(x) -> str:
    return "n/a" if x is None else f"{x:+,.2f}"


def format_daily_message(snap: dict, *, updated: bool = False) -> str:
    """Telegram text for a snapshot (Markdown, same style as the other notifier messages). Short on purpose."""
    t = (snap.get("today") or {}).get("overall")
    head = f"\U0001F4CA *Daily trade report{' (updated)' if updated else ''} \u2014 {snap.get('mode')} {snap.get('day')}*"
    if not t:
        return head + "\nNo closed trades today."
    charges = "n/a" if t.get("charges") is None else f"{t['charges']:,.2f}"
    lines = [head,
             f"{t['trades']} trades, {t['wins']} wins / {t['losses']} losses ({t['win_rate_pct']:.0f}%)",
             f"Gross \u20b9{_money(t['gross_pnl'])}, charges \u20b9{charges}, net \u20b9{_money(t['net_pnl'])}",
             f"Expectancy \u20b9{_money(t['expectancy'])} per trade"
             + (f", payoff {t['payoff_ratio']:.2f}" if t.get("payoff_ratio") is not None else "")]
    if t.get("avg_entry_slippage_pct") is not None:
        lines.append(f"Avg entry slippage vs signal {t['avg_entry_slippage_pct']:+.2f}%")
    by = (snap.get("today") or {}).get("by_exit_reason") or {}
    if by:
        parts = [f"{k} {v['trades']}x \u20b9{_money(v['expectancy'])}" for k, v in
                 sorted(by.items(), key=lambda kv: kv[1]["trades"] * kv[1]["expectancy"])]
        lines.append("By exit (expectancy): " + ", ".join(parts[:6]))
    w = (snap.get("week") or {}).get("overall")
    if w:
        lines.append(f"Last {snap.get('week_days')} days: {w['trades']} trades, expectancy \u20b9{_money(w['expectancy'])}"
                     f", win rate {w['win_rate_pct']:.0f}%")
    pending = charges_pending(snap)
    if pending:
        lines.append(f"(charges still estimated for {pending} of {t['trades']} trades)")
    if t.get("low_sample"):
        lines.append("(few trades today - treat as noise)")
    return "\n".join(lines)


def save_daily_snapshot(db, snap: dict) -> bool:
    """Store `snap` under its key. True only when THIS snapshot reads back, judged by its build time (the cache helper
    swallows write errors, and when a write over an older snapshot is lost the old one would still read back)."""
    from resilience import local_cache
    key = snapshot_key(snap["mode"], snap["day"])
    local_cache.save_snapshot(db, key, snap)
    back = local_cache.load_snapshot(db, key)
    return isinstance(back, dict) and back.get("generated_at") == snap.get("generated_at")


def load_daily_snapshot(db, mode: str, day: str) -> Optional[dict]:
    from resilience import local_cache
    return local_cache.load_snapshot(db, snapshot_key(mode, day))


def list_daily_snapshots(db, mode: str, limit: int = 30) -> list:
    """Stored days for `mode`, newest first: [{"day", "trades", "net_pnl", "expectancy", "generated_at"}]."""
    import json
    import models
    prefix = f"{SNAPSHOT_PREFIX}{mode}:"
    rows = (db.query(models.ResilienceCache).filter(models.ResilienceCache.key.like(prefix + "%"))
            .order_by(models.ResilienceCache.key.desc()).limit(max(1, min(int(limit), 365))).all())
    out = []
    for r in rows:
        if not str(r.key).startswith(prefix):        # LIKE treats "_" as a wildcard; keep exact prefixes only
            continue
        try:
            snap = json.loads(r.payload_json)
            o = (snap.get("today") or {}).get("overall") or {}
            out.append({"day": snap.get("day"), "trades": o.get("trades", 0), "net_pnl": o.get("net_pnl"),
                        "expectancy": o.get("expectancy"), "generated_at": snap.get("generated_at")})
        except Exception:  # noqa: BLE001
            continue
    return out
