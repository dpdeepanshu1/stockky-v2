"""
scripts/no_trade_diagnosis.py - 2026-10-07 (group 226)

"Why did no BUY or SELL happen today?"  READ-ONLY (no writes, no schema changes), safe to run against the live REAL
account while it trades.  It reads what the engine itself recorded and prints, for one IST day and one mode:

  1. the gate state (armed / auto-pilot / disarm reason)
  2. candidates queued vs evaluated, by source and decision label
  3. every ENTRY decision grouped by the reason it gave (numbers blanked so "R:R 1.81" and "R:R 1.79" are one line),
     i.e. which gate stopped the buys, in descending order
  4. the same for EXIT decisions on open positions (HOLD reasons, "No current price", exit-lock skips ...)
  5. orders actually sent (BUY/SELL, status), open positions and the watchlist's active/expired/missed counts

Usage (inside the real-trade-service container, where DATABASE_URL / ORACLE_DSN is set):
    docker compose exec real-trade-service python scripts/no_trade_diagnosis.py
    docker compose exec real-trade-service python scripts/no_trade_diagnosis.py --mode REAL --date 2026-10-07 --top 25
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

IST = timezone(timedelta(hours=5, minutes=30))
_NUM = re.compile(r"[-+]?\d[\d,]*\.?\d*")
_SYM_PREFIX = re.compile(r"^[A-Z0-9&\-]{2,20}: ")


def day_window_utc(day: str | None):
    """(start, end) of an IST calendar day as naive UTC datetimes (the DB stores naive UTC)."""
    d = datetime.strptime(day, "%Y-%m-%d").date() if day else datetime.now(IST).date()
    start = datetime(d.year, d.month, d.day, tzinfo=IST)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None), d


def normalize_reason(text: str | None, width: int = 110) -> str:
    """Blank the numbers so reasons that differ only in values group together."""
    t = (text or "(no reason recorded)").strip().replace("\n", " ")
    t = _SYM_PREFIX.sub("", t)
    t = _NUM.sub("#", t)
    t = re.sub(r"\s+", " ", t)
    return t[:width]


def group_reasons(rows, key_action=True):
    """rows: iterable of (action, reasoning). Returns Counter of (action, normalized reason)."""
    c: Counter = Counter()
    for action, reasoning in rows:
        c[(action or "?", normalize_reason(reasoning))] += 1
    return c


def _print_counter(title: str, counter: Counter, top: int) -> None:
    print(f"\n== {title} ==")
    if not counter:
        print("   (none)")
        return
    for (action, reason), n in counter.most_common(top):
        print(f"{n:6d}  {action:<14} {reason}")
    rest = sum(counter.values()) - sum(n for _, n in counter.most_common(top))
    if rest > 0:
        print(f"{rest:6d}  ... {len(counter) - top} more distinct reasons")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="REAL", choices=["REAL", "DEMO"])
    ap.add_argument("--date", default=None, help="IST day YYYY-MM-DD (default: today)")
    ap.add_argument("--top", type=int, default=20, help="reasons shown per section")
    a = ap.parse_args(argv)

    import models
    from db import get_session_factory

    start, end, day = day_window_utc(a.date)
    db = get_session_factory()()
    try:
        print(f"No-trade diagnosis  mode={a.mode}  IST day={day}  (UTC window {start} .. {end})")

        g = db.query(models.TradeGateState).filter_by(mode=a.mode).first()
        print("\n== gate state ==")
        if g is None:
            print("   no TradeGateState row for this mode (never armed)")
        else:
            print(f"   armed={g.armed} armed_at={g.armed_at} disarmed_reason={g.disarmed_reason!r}")
            print(f"   admin_authenticated={g.admin_authenticated} (expires {g.admin_session_expires_at}) "
                  f"dhan_connected={g.dhan_connected} risk_config_confirmed={g.risk_config_confirmed}")
            for col in ("auto_pilot_enabled", "auto_pilot", "autopilot_enabled"):
                if hasattr(g, col):
                    print(f"   {col}={getattr(g, col)}")

        C, D, O, P = models.TradeCandidate, models.TradeDecision, models.TradeOrder, models.TradePosition
        cands = db.query(C).filter(C.mode == a.mode, C.received_at >= start, C.received_at < end).all()
        print(f"\n== candidates queued today: {len(cands)} (evaluated/consumed: {sum(1 for c in cands if c.consumed)}) ==")
        by = Counter((c.source_tab or "?", (c.decision_label or "?")) for c in cands)
        for (tab, label), n in by.most_common(a.top):
            print(f"{n:6d}  {tab:<14} {label}")
        left = db.query(C).filter_by(mode=a.mode, consumed=False).count()   # filter_by: Oracle-safe `= 0` (group150)
        print(f"   still queued (not yet evaluated, any day): {left}")

        entry = db.query(D.action, D.reasoning).filter(
            D.mode == a.mode, D.decision_type == "ENTRY", D.created_at >= start, D.created_at < end).all()
        _print_counter(f"ENTRY decisions today ({len(entry)}) by reason - this is what stopped the buys",
                       group_reasons(entry), a.top)
        exits = db.query(D.action, D.reasoning).filter(
            D.mode == a.mode, D.decision_type == "EXIT", D.created_at >= start, D.created_at < end).all()
        _print_counter(f"EXIT decisions today ({len(exits)}) by reason - this is what stopped the sells",
                       group_reasons(exits), a.top)

        orders = db.query(O.side, O.status).filter(O.mode == a.mode, O.created_at >= start, O.created_at < end).all()
        print(f"\n== orders created today: {len(orders)} ==")
        for (side, status), n in Counter(orders).most_common():
            print(f"{n:6d}  {side:<5} {status}")

        open_pos = db.query(P).filter(P.mode == a.mode, P.status.in_(["OPEN", "PARTIALLY_CLOSED"])).all()
        print(f"\n== open positions now: {len(open_pos)} ==")
        for p in open_pos[:30]:
            print(f"   {p.symbol:<14} qty_open={p.qty_open} entry={p.avg_entry_price} stop={p.current_stop} opened={p.opened_at}")
        closed = db.query(P).filter(P.mode == a.mode, P.closed_at >= start, P.closed_at < end).count()
        print(f"   closed today: {closed}")

        W = models.WatchlistEntry
        wl = Counter(r[0] for r in db.query(W.status).filter(W.mode == a.mode, W.created_at >= start, W.created_at < end).all())
        print(f"\n== watchlist rows created today by status: {dict(wl) or '(none)'} ==")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
