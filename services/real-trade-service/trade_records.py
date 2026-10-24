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

from tz_utils import as_aware, ist_now, iso_utc

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


# ── exit-reason attribution and hold length (group 421) ────────────────────────────────────────────────────────────────
# A record's exit_reason came only from the closing SELL order. Positions closed WITHOUT such an order (the holdings sync force-closing a
# position the broker no longer holds, a DEMO close, a SELL fill that did not match the position by time) showed "unknown" - 2 of the 5
# one-day trades in the 16 Oct export. Every one of those paths already writes a TradePositionEvent, so the reason is now read from
# there as a fallback (no new column, so it also repairs old records). `exit_reason_source` says where the reason came from.
_GENERIC_REASONS = ("exit", "sell", "closed", "close")
EVENT_CLOSE_TYPES = ("CLOSED", "EXIT_SENT", "GHOST_CLOSED")
HOLD_BUCKETS = ("same_day", "1_day", "2_5_days", "6_plus_days")


def _event_reason(events) -> tuple:
    """(reason, source) from one position's own events, newest closing event first; (None, None) when none says why. Pure; never raises."""
    try:
        evs = [e for e in (events or []) if str(getattr(e, "event_type", "") or "").upper() in EVENT_CLOSE_TYPES]
        evs.sort(key=lambda e: as_aware(getattr(e, "occurred_at", None)) or datetime.min.replace(tzinfo=timezone.utc))
        for e in reversed(evs):
            kind = str(e.event_type).upper()
            detail = str(getattr(e, "detail", "") or "")
            if kind == "GHOST_CLOSED":
                low = detail.lower()
                if "sold at the broker" in low or "sold outside" in low or "externally" in low:
                    return "sold_outside_app", "event_ghost_closed"
                return "ghost_closed", "event_ghost_closed"
            head = _norm_reason(detail.split(":", 1)[0]) if ":" in detail else None
            if head and head not in _GENERIC_REASONS and " " not in head and len(head) <= 32:
                return head, "event_closed" if kind == "CLOSED" else "event_exit_sent"
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _is_nse_holiday_str(day: str) -> bool:
    try:
        from tz_utils import _NSE_HOLIDAYS_2026
        return day in _NSE_HOLIDAYS_2026
    except Exception:  # noqa: BLE001
        return False


def trading_days_held(opened_at, closed_at) -> Optional[int]:
    """Trading sessions after the entry's IST date up to and including the exit's IST date: 0 = same day, 1 = next session.
    Weekends and the NSE holidays in tz_utils are skipped. None when either time is missing or the exit is before the entry. Pure."""
    try:
        if opened_at is None or closed_at is None:
            return None
        d0 = ist_now(as_aware(opened_at)).date()
        d1 = ist_now(as_aware(closed_at)).date()
        if d1 < d0:
            return None
        n, d = 0, d0
        for _ in range(400):
            if d >= d1:
                break
            d = d + timedelta(days=1)
            if d.weekday() < 5 and not _is_nse_holiday_str(d.isoformat()):
                n += 1
        return n
    except Exception:  # noqa: BLE001
        return None


def hold_bucket(days) -> Optional[str]:
    if days is None:
        return None
    return "same_day" if days <= 0 else "1_day" if days == 1 else "2_5_days" if days <= 5 else "6_plus_days"


def _return_pct(entry, qty, net, gross, exit_price) -> tuple:
    """(percent, basis): net P&L over the cost basis when the size is known, else the gross price move; (None, None) when neither."""
    if entry and entry > 0 and qty and qty > 0 and net is not None:
        return round(net / (entry * qty) * 100.0, 3), "net"
    if entry and entry > 0 and exit_price and exit_price > 0:
        return round((exit_price / entry - 1.0) * 100.0, 3), "gross_price"
    return None, None


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


def _entry_qty(buys: list, fills_by_order: dict) -> Optional[int]:
    """group 398: shares bought to open the position - the filled quantity of its BUY orders, else the orders' own quantity. None when unknown."""
    filled = sum(_num(f.qty) or 0.0 for o in buys for f in fills_by_order.get(o.id, []))
    if filled > 0:
        return int(round(filled))
    ordered = sum(_num(getattr(o, "qty", None)) or 0.0 for o in buys)
    return int(round(ordered)) if ordered > 0 else None


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


def _bot_entry_behind_import(pos, entry_order, fills_by_order: dict) -> bool:
    """group438: a `broker_imported` position whose entry is really this service's own filled AUTO BUY (a Dhan order id, booked within
    _ORDER_SLACK_BEFORE_S of opened_at). Before group 438 the reconcile loop imported a just-filled bot CNC entry before booking its
    fill, so these rows look like imports although the bot chose and placed them."""
    if not getattr(pos, "broker_imported", False) or entry_order is None:
        return False
    if str(getattr(entry_order, "execution_source", "") or "").upper() != "AUTO" or not getattr(entry_order, "dhan_order_id", None):
        return False
    t = _order_time(entry_order, fills_by_order)
    if t is None:
        return False
    return abs((as_aware(pos.opened_at) - t).total_seconds()) <= _ORDER_SLACK_BEFORE_S


def _log_only_ctx(cand) -> dict:
    """group468: the log-only Mansfield RS / breadth block the candidate engine stored in the candidate's raw_payload["_log_only"].
    Record only: nothing gates on it. All None when absent (candidates made before group 468) or unparseable. Never raises."""
    out = {"mansfield_rs": None, "above_ema50": None, "breadth_pct": None}
    try:
        import json as _json
        raw = getattr(cand, "raw_payload", None)
        payload = _json.loads(raw) if isinstance(raw, str) and raw else (raw if isinstance(raw, dict) else {})
        blk = (payload or {}).get("_log_only") or {}
        out["mansfield_rs"] = _num(blk.get("mansfield_rs"))
        out["breadth_pct"] = _num(blk.get("breadth_pct"))
        a = blk.get("above_ema50")
        out["above_ema50"] = a if isinstance(a, bool) else None
    except Exception:  # noqa: BLE001
        pass
    return out


_NON_EXIT_DECISIONS = ("HOLD", "TRAIL_STOP")


def _exit_trigger(pos, reason, exit_decisions, exit_price) -> dict:
    """group469: what the exit was triggered at, and how far the fill landed from it. Source order: (1) the LTP stored on the last
    real exit decision of this position (HOLD / TRAIL_STOP rows are not exits) = 'decision_ltp'; (2) for a stop exit with no
    decision (the broker-side stop fired on its own) the position's final stop = 'final_stop'. + = filled ABOVE the trigger
    (better for a long exit), - = worse. All None when neither exists. Pure; never raises."""
    out = {"exit_trigger_price": None, "exit_trigger_source": None, "exit_slippage_pct": None}
    try:
        trig, src = None, None
        rows = [d for d in (exit_decisions or ()) if str(getattr(d, "action", "")).upper() not in _NON_EXIT_DECISIONS
                and (_num(getattr(d, "ltp_at_decision", None)) or 0) > 0]
        if rows:
            last = max(rows, key=lambda d: (as_aware(d.evaluated_at) if getattr(d, "evaluated_at", None) else 0, getattr(d, "id", 0) or 0))
            trig, src = _num(last.ltp_at_decision), "decision_ltp"
        elif reason in STOP_REASONS:
            cs = _num(getattr(pos, "current_stop", None))
            if cs and cs > 0:
                trig, src = cs, "final_stop"
        if trig and trig > 0:
            out["exit_trigger_price"], out["exit_trigger_source"] = round(trig, 4), src
            if exit_price and exit_price > 0:
                out["exit_slippage_pct"] = round((exit_price - trig) / trig * 100.0, 3)
    except Exception:  # noqa: BLE001
        pass
    return out


_ATTR_FIELDS = ("source_tab", "decision_label", "stop_basis", "imbalance_5")


def _market_stamp(pos, entry_order) -> dict:
    """P5: the market state and profile version in force when the trade was opened. The position's own column first (copied from the
    entry order at fill), then the entry order itself (a position created by the broker-import path has it NULL). Pure, never raises;
    nothing recorded stays None (never invented, never a default state)."""
    out = {}
    for key in ("market_state", "profile_version"):
        val = None
        try:
            for src in (pos, entry_order):
                v = getattr(src, key, None) if src is not None else None
                if isinstance(v, str) and v.strip():
                    val = v.strip()
                    break
        except Exception:  # noqa: BLE001
            val = None
        out[key] = val
    return out


def _attribution(pos, entry_order, cand, decision) -> dict:
    """A13: source_tab / decision_label / conviction with a fallback chain, plus where each value came from. The position's own
    column is set only when the fill creates the row; a position first created by the broker-import path (the group-438 race) or
    opened before group 396 has it NULL even though the entry ORDER and CANDIDATE recorded it. Order: position -> entry order ->
    candidate; a manual entry with nothing recorded is labelled 'manual'. Pure, never raises; unknown stays None (never invented)."""
    def first(*pairs):
        for val, src in pairs:
            if val not in (None, ""):
                return val, src
        return None, None
    try:
        tab, tab_src = first((getattr(pos, "source_tab", None), "position"),
                             (getattr(entry_order, "source_tab", None), "order"),
                             (getattr(cand, "source_tab", None), "candidate"))
        if tab is None and entry_order is not None and str(getattr(entry_order, "execution_source", "") or "").upper() == "MANUAL":
            tab, tab_src = "manual", "manual_order"
        label, label_src = first((getattr(pos, "entry_decision_label", None), "position"),
                                 (getattr(entry_order, "entry_decision_label", None), "order"),
                                 (getattr(cand, "decision_label", None), "candidate"))
        conv, _ = first((getattr(pos, "entry_conviction_score", None), "position"),
                        (getattr(entry_order, "entry_conviction_score", None), "order"),
                        (getattr(cand, "conviction_score", None), "candidate"))
        return {"source_tab": tab, "decision_label": label, "conviction": _num(conv),
                "attribution_source": {"source_tab": tab_src, "decision_label": label_src}}
    except Exception:  # noqa: BLE001
        return {"source_tab": None, "decision_label": None, "conviction": None, "attribution_source": {}}


def attribution_coverage(records: list) -> dict:
    """A13: how many records carry each attribution field, overall and for system-entered trades only. Pure."""
    def cov(rows):
        n = len(rows)
        return {f: {"filled": sum(1 for r in rows if r.get(f) not in (None, "")), "total": n,
                    "pct": (round(100.0 * sum(1 for r in rows if r.get(f) not in (None, "")) / n, 1) if n else None)}
                for f in _ATTR_FIELDS + ("tier_name",)}
    sysrows = [r for r in records if r.get("origin") == "system"]
    return {"all": cov(records), "system_entered": cov(sysrows),
            "note": "tier_name is empty by design for trades that did not come from a watchlist entry; stop_basis and imbalance_5 are "
                    "written at the entry decision, so trades entered before groups 396 / 427 stay empty."}


def build_records(positions: Iterable, orders: Iterable, fills: Iterable, decisions: Iterable, candidates: Iterable,
                  watchlist: Iterable, ledger: Iterable, events: Iterable = (), include_imported: bool = False,
                  exit_decisions: Iterable = ()) -> list:
    """Pure: one dict per CLOSED, system-entered position that has a realized P&L (see module docstring).
    `events` (group 421, optional): TradePositionEvent rows; the exit reason falls back to them when no closing SELL order carries one."""
    exit_dec_by_pos: dict = defaultdict(list)           # group469: TradeExitDecision rows, for the exit trigger price
    for xd in exit_decisions or ():
        exit_dec_by_pos[getattr(xd, "position_id", None)].append(xd)
    events_by_pos: dict = defaultdict(list)
    for ev in events or ():
        events_by_pos[getattr(ev, "position_id", None)].append(ev)
    closed_all = [p for p in positions
                  if getattr(p, "status", None) == "CLOSED" and getattr(p, "realized_pnl", None) is not None
                  and getattr(p, "opened_at", None) is not None]
    closed = closed_all   # narrowed below, after the orders are matched (group438: imported rows must take part in the matching)
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
    assigned = _assign(closed_all, orders, fills_by_order)
    # group433: include_imported opts the broker-imported rows in; group438: a bot entry that was booked through the import path is not an import
    closed = [p for p in closed_all
              if include_imported or not getattr(p, "broker_imported", False)
              or _bot_entry_behind_import(p, (assigned[p.id]["buys"] or [None])[0], fills_by_order)]

    out = []
    for p in sorted(closed, key=lambda p: as_aware(p.closed_at or p.opened_at)):
        try:
            a = assigned[p.id]
            buys, sells = a["buys"], a["sells"]
            entry_order = buys[0] if buys else None
            closing_sell = sells[-1] if sells else None
            decision = decisions_by_id.get(getattr(entry_order, "decision_id", None)) if entry_order else None
            cand = cand_by_id.get(getattr(decision, "candidate_id", None)) if decision else None
            w_id = (getattr(p, "watchlist_entry_id", None) or (getattr(entry_order, "watchlist_entry_id", None) if entry_order else None)
                    or getattr(cand, "watchlist_entry_id", None))   # A13: candidate as the last fallback
            attr = _attribution(p, entry_order, cand, decision)
            ms = _market_stamp(p, entry_order)
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
            reason, reason_src = None, None
            if closing_sell is not None:
                reason = _norm_reason(getattr(closing_sell, "exit_reason", None))
                if reason is not None:
                    reason_src = "order"
                elif str(getattr(closing_sell, "execution_source", "") or "").upper() == "MANUAL":
                    reason, reason_src = "manual", "order_manual"
            if reason is None:                                     # group 421: the position's own events say why it closed
                reason, reason_src = _event_reason(events_by_pos.get(p.id))
            if reason is None and closing_sell is not None:
                reason, reason_src = "unspecified_sell", "order_without_reason"   # a SELL filled but nothing recorded why
            if reason is None:
                reason_src = "none"
            tier = getattr(w, "source_tier", None)
            # group 396: the stop the trade was opened with. initial_stop_distance is fixed at the opening fill (never trailed); the decision's
            # proposed stop is the fallback for rows without it. stop_basis comes from the entry decision (atr / flat / structure; None before group 396).
            isd = _num(getattr(p, "initial_stop_distance", None))
            if isd is not None and isd > 0 and entry and entry > 0:
                stop_pct = round(isd / entry * 100.0, 3)
            else:
                pp, ps = _num(getattr(decision, "proposed_price", None)), _num(getattr(decision, "proposed_stop", None))
                stop_pct = round((pp - ps) / pp * 100.0, 3) if pp and ps and pp > ps > 0 else None
            exc = _excursion_pcts(entry, getattr(p, "max_price_seen", None), getattr(p, "min_price_seen", None))   # group 397
            stop_basis = getattr(decision, "stop_basis", None)
            stop_basis = stop_basis if isinstance(stop_basis, str) and stop_basis else None
            from entry_engine import order_flow as _of          # group 427: order-flow numbers recorded at the entry decision
            flow = _of.parse(getattr(decision, "flow_json", None))
            held = None
            if p.closed_at is not None:
                held = round((as_aware(p.closed_at) - as_aware(p.opened_at)).total_seconds() / 60.0, 1)
            qty_in = _entry_qty(buys, fills_by_order)
            days_held = trading_days_held(p.opened_at, p.closed_at)
            ret_pct, ret_basis = _return_pct(entry, qty_in, net, gross, exit_price)
            out.append({
                "position_id": p.id, "mode": p.mode, "symbol": p.symbol,
                "origin": "imported" if (getattr(p, "broker_imported", False) and not _bot_entry_behind_import(p, entry_order, fills_by_order)) else "system",   # group433; group438: bot entries recorded as imports count as system
                "import_race": bool(_bot_entry_behind_import(p, entry_order, fills_by_order)),   # group438: True = the bot's own BUY, booked through the broker-import path
                "source_tab": attr["source_tab"],                    # A13: position -> entry order -> candidate (was position only)
                "decision_label": attr["decision_label"],
                "conviction": attr["conviction"],
                "attribution_source": attr["attribution_source"],
                "regime_override": bool(getattr(p, "is_regime_override", False)),
                "market_state": ms["market_state"], "profile_version": ms["profile_version"],   # P5: Market Weather stamp, position -> entry order; None = none was active (never 0)
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
                "exit_reason": reason, "exit_reason_source": reason_src, "exits": len(sells), "entries": len(buys),
                "held_days": days_held, "hold_bucket": hold_bucket(days_held),     # group 421: sessions held (0 = same day); see scripts/hold_bucket_check.py
                "return_pct": ret_pct, "return_basis": ret_basis,
                "gross_pnl": gross, "charges": charges, "charges_source": charges_src, "net_pnl": net,
                "orders_matched": bool(buys or sells),
                "stop_pct": stop_pct, "stop_basis": stop_basis,
                "imbalance_5": flow.get("imbalance_5"), "imbalance_20": flow.get("imbalance_20"),
                "flow_spread_pct": flow.get("spread_pct"), "flow_book_value_5": flow.get("book_value_5"),
                "flow_vs_vwap_pct": flow.get("vs_vwap_pct"), "flow_day_pos": flow.get("day_pos"),
                **_orb_fields(flow.get("orb")),                                  # group 474: opening-range shadow verdict (record only)
                "max_gain_pct": exc["max_gain_pct"], "max_drawdown_pct": exc["max_drawdown_pct"],
                **_exit_trigger(p, reason, exit_dec_by_pos.get(p.id), exit_price),     # group469: exit_trigger_price / _source / exit_slippage_pct
                **{("log_" + k): v for k, v in _log_only_ctx(cand).items()},     # group468: log_mansfield_rs / log_above_ema50 / log_breadth_pct (record only)
                # group 398: what scripts/opening_replay.py needs to replay a real-trade export (opening time, close time, size)
                "opened_at": iso_utc(p.opened_at), "closed_at": iso_utc(p.closed_at), "quantity": qty_in,
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("trade record for position %s skipped: %s: %s", getattr(p, "id", "?"), type(e).__name__, e)
    return out


def _rs_bucket(v) -> str:
    """group468: Mansfield RS bucket for the evidence report (log-only)."""
    x = _num(v)
    if x is None:
        return "unknown"
    return "rs<0" if x < 0 else ("rs 0-2" if x < 2 else "rs>=2")


def _breadth_bucket(v) -> str:
    """group468: market-breadth bucket (% of the engine's universe above its 50-day EMA; log-only)."""
    x = _num(v)
    if x is None:
        return "unknown"
    return "breadth<35" if x < 35 else ("breadth 35-65" if x <= 65 else "breadth>65")


def _orb_fields(orb) -> dict:
    """group 474: flat opening-range-breakout shadow fields from flow_json["orb"] (entry_engine/orb_shadow.py). Pure, never raises.
    orb_would_enter: True / False / None (no verdict recorded: entry outside 09:15-10:30 or made before group 470).
    E113: orb_close_trigger (True / False / None = no bar fields recorded) and orb_volume_ratio (trigger-bar volume / opening-range average, None when not recorded)."""
    empty = {"orb_would_enter": None, "orb_reasons": None, "orb_range_pct": None, "orb_close_trigger": None, "orb_volume_ratio": None}
    try:
        if not isinstance(orb, dict):
            return dict(empty)
        rs = orb.get("reasons")
        first = (rs[0] if isinstance(rs, list) and rs else orb.get("reason"))
        ct = orb.get("close_trigger")
        return {"orb_would_enter": (bool(orb["would_enter"]) if "would_enter" in orb else None),
                "orb_reasons": first, "orb_range_pct": orb.get("range_pct"),
                "orb_close_trigger": (ct if isinstance(ct, bool) else None), "orb_volume_ratio": orb.get("volume_ratio")}
    except Exception:  # noqa: BLE001
        return dict(empty)


def _orb_bucket(r) -> str:
    v = r.get("orb_would_enter")
    if v is None:
        return "no_verdict"
    return "orb_would_enter" if v else "orb_would_skip:" + str(r.get("orb_reasons") or "?")


def _orb_trigger_bucket(r) -> str:
    """E113: did the first completed bar after the range close above the range high? 'no_orb_record' when no bar fields were recorded (never counted as 'no trigger')."""
    v = r.get("orb_close_trigger")
    return "no_orb_record" if v is None else ("close_trigger" if v else "no_close_trigger")


def _orb_volume_bucket(r) -> str:
    """E113: trigger-bar volume / average opening-range bar volume, in the same buckets as Position Stocks; 'none' when not recorded."""
    raw = r.get("orb_volume_ratio")
    try:
        v = float(raw) if raw is not None and not isinstance(raw, bool) else None
    except (TypeError, ValueError):
        v = None
    if v is None or v != v:
        return "none"
    return "<1.0" if v < 1.0 else "1.0-1.5" if v < 1.5 else "1.5-2.5" if v < 2.5 else ">=2.5"


def _imb_bucket(v) -> str:
    from entry_engine.order_flow import imbalance_bucket
    return imbalance_bucket(v)


def _spread_bucket(v) -> str:
    from entry_engine.order_flow import spread_bucket
    return spread_bucket(v)


def _excursion_pcts(entry, hi, lo) -> dict:
    """group 397: max gain / max drawdown % vs entry from the stored peak / trough (None when either is missing). Pure."""
    from exit_engine.excursion import excursion_pcts
    return excursion_pcts(entry, hi, lo)


STOP_REASONS = ("stop_hit", "broker_stop", "emergency_gap_down")      # exits that are the stop itself (group 394 definition)


def stop_diagnostics(records: list, mults=(0.5, 0.75)) -> dict:
    """group 397: stop indicators per stop basis (and `all`) over the records that carry their own stop AND an excursion.
    stop_outs = exits by STOP_REASONS; stopped_after_running_half_r = stop-outs that had been up >= 0.5R first; survivors_within_02r =
    other trades whose drawdown came within 0.2R of the stop; tighter[k] = trades whose drawdown reached k x their stop (a tighter stop
    would have fired) and, of the non-stop-out ones, how many were winners (the winners a tighter stop would have killed).
    Counts only - no P&L what-if (a record has no quantity). Pure."""
    usable = [r for r in records if (r.get("stop_pct") or 0) > 0 and r.get("max_gain_pct") is not None and r.get("max_drawdown_pct") is not None]
    groups: dict = {"all": usable}
    for r in usable:
        groups.setdefault(r.get("stop_basis") or "unknown", []).append(r)
    out: dict = {"trades": len(records), "with_stop_and_excursion": len(usable), "mults": list(mults), "groups": {}}
    for name, rows in groups.items():
        stopped = [r for r in rows if r.get("exit_reason") in STOP_REASONS]
        rest = [r for r in rows if r.get("exit_reason") not in STOP_REASONS]
        tighter = []
        for k in mults:
            hit = [r for r in rows if r["max_drawdown_pct"] <= -k * r["stop_pct"]]
            hit_rest = [r for r in hit if r.get("exit_reason") not in STOP_REASONS]
            tighter.append({"mult": k, "would_stop": len(hit), "non_stop_out_trades_hit": len(hit_rest),
                            "winners_killed": sum(1 for r in hit_rest if _pnl_used(r) > _EPS)})
        out["groups"][name] = {
            "trades": len(rows), "stop_outs": len(stopped),
            "stopped_after_running_half_r": sum(1 for r in stopped if r["max_gain_pct"] >= 0.5 * r["stop_pct"]),
            "survivors_within_02r": sum(1 for r in rest if r["max_drawdown_pct"] <= -0.8 * r["stop_pct"]),
            "tighter": tighter,
        }
    return out


def _pnl_used(r: dict) -> float:
    return r["net_pnl"] if r.get("net_pnl") is not None else r["gross_pnl"]


def _mean(xs: list) -> Optional[float]:
    return round(sum(xs) / len(xs), 2) if xs else None


def exit_slippage_summary(records: list) -> dict:
    """group469: exit fill vs the stored exit trigger, overall and per exit reason. {} when no record has one. Pure."""
    by: dict = defaultdict(list)
    for r in records or ():
        v = _num(r.get("exit_slippage_pct"))
        if v is not None:
            by[r.get("exit_reason") or "unknown"].append(v)
    if not by:
        return {}
    allv = [v for vs in by.values() for v in vs]
    f = lambda vs: {"n": len(vs), "mean": round(sum(vs) / len(vs), 3), "worst": round(min(vs), 3), "best": round(max(vs), 3)}  # noqa: E731
    return {"overall": f(allv), "by_exit_reason": {k: f(v) for k, v in sorted(by.items())}}


def performance_block(records: list) -> dict:
    """Whole-book performance on a daily net-P&L basis (additive rupees, not compounded): cumulative net per exit day (IST),
    max drawdown in rupees with the peak and trough days, days spent underwater, Return/MaxDD, longest run of winning days
    and the best single day. Pure; never raises."""
    by_day: dict = defaultdict(float)
    try:
        for r in records:
            day = r.get("exit_day_ist") or str(r.get("closed_at") or "")[:10]
            v = _num(r.get("net_pnl"))
            if day and v is not None:
                by_day[day] += v
    except Exception:  # noqa: BLE001
        pass
    days = sorted(by_day)
    if not days:
        return {"days": 0, "equity": [], "max_drawdown": None, "drawdown_start": None, "drawdown_end": None,
                "days_in_drawdown": None, "return_maxdd": None, "max_win_streak": None, "best_day": None, "total_net": None}
    equity, cum = [], 0.0
    for d in days:
        cum += by_day[d]
        equity.append((d, round(cum, 2)))
    peak, peak_day = 0.0, None          # the book starts at 0 before the first day, so a first-day loss is a drawdown
    mdd, mdd_start, mdd_end, underwater = 0.0, None, None, 0
    for d, c in equity:
        if c > peak:
            peak, peak_day = c, d
        elif c < peak:
            underwater += 1
            if peak - c > mdd + 1e-9:
                mdd, mdd_start, mdd_end = peak - c, peak_day, d
    total = equity[-1][1]
    streak = best_streak = 0
    for d in days:
        if by_day[d] > 0:
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            streak = 0
    best_day = max(days, key=lambda d: by_day[d])        # ties -> earliest (days is sorted)
    return {
        "days": len(days),
        "equity": [{"day": d, "cum_net": c} for d, c in equity],
        "max_drawdown": round(mdd, 2),
        "drawdown_start": mdd_start if mdd > 0 else None,   # None = the book never fell below a prior high
        "drawdown_end": mdd_end,
        "days_in_drawdown": underwater,
        "return_maxdd": round(total / mdd, 2) if mdd > 0 else None,
        "max_win_streak": best_streak,
        "best_day": {"day": best_day, "net": round(by_day[best_day], 2)},
        "total_net": total,
    }


def _agg(rows: list, min_trades_note: int) -> dict:
    used = [_pnl_used(r) for r in rows]
    wins = [x for x in used if x > _EPS]
    losses = [x for x in used if x < -_EPS]
    avg_win, avg_loss = _mean(wins), _mean(losses)
    nets = [r["net_pnl"] for r in rows if r.get("net_pnl") is not None]
    charges = [r["charges"] for r in rows if r.get("charges") is not None]
    slips = [r["entry_slippage_pct"] for r in rows if r.get("entry_slippage_pct") is not None]
    held = [r["held_minutes"] for r in rows if r.get("held_minutes") is not None]
    gross_sum = round(sum(r["gross_pnl"] for r in rows), 2)
    charge_sum = round(sum(charges), 2) if charges else None
    return {
        "trades": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(rows), 1),
        "gross_pnl": gross_sum,
        "charges": charge_sum,
        "charges_share_of_gross_profit_pct": (round(100.0 * charge_sum / gross_sum, 1)
                                              if charge_sum is not None and gross_sum > 0 else None),
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
        "by_source_tab": {}, "by_day": {}, "by_stop_basis": {}, "by_imbalance_5": {}, "by_imbalance_20": {}, "by_spread": {},
        "by_mansfield_rs": {}, "by_breadth": {},
        "by_market_state": {}, "by_profile_version": {},
        "stop_diagnostics": None, "attribution_coverage": attribution_coverage(records),
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
        "by_stop_basis": lambda r: r.get("stop_basis") or "unknown",
        # group 427: order-flow buckets (entries made before the record existed fall in "unknown")
        "by_imbalance_5": lambda r: _imb_bucket(r.get("imbalance_5")),
        "by_imbalance_20": lambda r: _imb_bucket(r.get("imbalance_20")),
        "by_spread": lambda r: _spread_bucket(r.get("flow_spread_pct")),
        # group468: log-only evidence buckets (candidates made before the record existed fall in "unknown")
        "by_orb_shadow": _orb_bucket,                                     # group474: would an opening-range entry have taken this trade?
        "by_orb_close_trigger": _orb_trigger_bucket,                      # E113: close-based ORB trigger (needs 1-minute bars; absent = no_orb_record)
        "by_orb_volume_ratio": _orb_volume_bucket,                        # E113: trigger-bar volume vs the opening-range average
        "by_mansfield_rs": lambda r: _rs_bucket(r.get("log_mansfield_rs")),
        "by_breadth": lambda r: _breadth_bucket(r.get("log_breadth_pct")),
        # P5: Market Weather stamp (trades opened with no state active, or before the stamp existed, fall in "none")
        "by_market_state": lambda r: r.get("market_state") or "none",
        "by_profile_version": lambda r: r.get("profile_version") or "none",
    }
    for name, key in keyers.items():
        groups: dict = defaultdict(list)
        for r in records:
            groups[key(r)].append(r)
        out[name] = {k: _agg(v, min_trades_note) for k, v in sorted(groups.items())}
    out["stop_diagnostics"] = stop_diagnostics(records)
    out["performance"] = performance_block(records)
    out["exit_slippage"] = exit_slippage_summary(records)
    return out


def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_records(db, mode: str, days: int = 7, symbol: Optional[str] = None, include_imported: bool = False) -> list:
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
    events: list = []
    try:                                                    # group 421: closing events explain positions that closed without a SELL order
        for part in _chunks(sorted(p.id for p in positions)):
            events += (db.query(models.TradePositionEvent)
                       .filter(models.TradePositionEvent.position_id.in_(part),
                               models.TradePositionEvent.event_type.in_(EVENT_CLOSE_TYPES)).all())
    except Exception as e:  # noqa: BLE001 - attribution is best effort; the records themselves must still load
        logger.warning("trade records: position events not loaded (%s: %s) - exit reasons come from orders only", type(e).__name__, e)
        events = []
    exit_decisions: list = []
    try:                                                    # group469: the LTP at each real exit decision = the exit trigger price
        for part in _chunks(sorted(p.id for p in positions)):
            exit_decisions += (db.query(models.TradeExitDecision)
                               .filter(models.TradeExitDecision.position_id.in_(part),
                                       ~models.TradeExitDecision.action.in_(_NON_EXIT_DECISIONS)).all())
    except Exception as e:  # noqa: BLE001 - best effort; records must still load
        logger.warning("trade records: exit decisions not loaded (%s: %s) - no exit trigger prices", type(e).__name__, e)
        exit_decisions = []
    return build_records(positions, orders, fills, decisions, candidates, watchlist, ledger, events, include_imported=include_imported,
                         exit_decisions=exit_decisions)


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
        # group469: drawdown over the rolling window (NOT the whole book: that is GET /records expectancy / scripts/records_report.py)
        "week_performance": {k: v for k, v in performance_block(recs).items() if k != "equity"},
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
    if t.get("gross_pnl") and t["gross_pnl"] > 0 and t.get("charges"):
        lines.append(f"Charges were {100.0 * t['charges'] / t['gross_pnl']:.0f}% of the gross profit")
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
    wp = snap.get("week_performance") or {}
    if wp.get("days", 0) >= 2 and wp.get("max_drawdown"):
        lines.append(f"Last {snap.get('week_days')} days drawdown \u20b9{_money(wp['max_drawdown'])} "
                     f"({wp.get('drawdown_start') or 'start'} to {wp.get('drawdown_end')}), net \u20b9{_money(wp.get('total_net'))}")
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
