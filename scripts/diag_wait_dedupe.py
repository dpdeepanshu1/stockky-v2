# Round 671 (D3 check): is the WAIT-row dedupe working? Read-only, ORM only (no hand-written Oracle SQL), light (indexed on mode/type/created_at).
# Run from ~/stockky-v2, no file to save:
#   docker compose exec -T real-trade-service python3 - < scripts/diag_wait_dedupe.py
# Wider window:  docker compose exec -T -e WINDOW_MIN=120 real-trade-service python3 - < scripts/diag_wait_dedupe.py   (default 30 min)
# Prints, for REAL ENTRY decision rows: (1) rows today and in the last WINDOW_MIN minutes by action, (2) rows vs distinct symbols,
# (3) the symbol + reason pairs with the most rows, with the SMALLEST gap between two rows of the same pair (dedupe window is
# WAIT_DEDUPE_MINUTES, default 15: a smallest gap far below it means repeats are still being written).
import os
import sys

sys.path.insert(0, "/app")
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import db
import models
from sqlalchemy import func

WINDOW_MIN = int(os.environ.get("WINDOW_MIN", "30") or 30)
DEDUPE_MIN = float(os.environ.get("WAIT_DEDUPE_MINUTES", "15") or 15)
IST = timezone(timedelta(hours=5, minutes=30))


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _reason(text):
    return " ".join((text or "-").split())[:70]


def main():
    now = datetime.now(timezone.utc)
    day_start = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    since = now - timedelta(minutes=WINDOW_MIN)
    D = models.TradeDecision
    with db.get_engine().connect() as conn:
        from sqlalchemy.orm import Session
        s = Session(bind=conn)
        base = (D.mode == "REAL", D.decision_type == "ENTRY")
        for label, t0 in (("today (IST day)", day_start), (f"last {WINDOW_MIN} min", since)):
            print(f"1) REAL ENTRY rows, {label}")
            rows = (s.query(D.action, func.count(D.id), func.count(func.distinct(D.symbol)))
                    .filter(*base, D.created_at >= t0).group_by(D.action).order_by(func.count(D.id).desc()).all())
            for a, n, sy in rows:
                print(f"   {a:<8} rows={n:>6}  symbols={sy:>5}")
            if not rows:
                print("   (no rows)")
        print(f"\n2) WAIT rows per symbol + reason, last {WINDOW_MIN} min: most repeated pairs (dedupe window {DEDUPE_MIN:g} min)")
        waits = (s.query(D.symbol, D.reasoning, D.created_at)
                 .filter(*base, D.action == "WAIT", D.created_at >= since).order_by(D.created_at).limit(3000).all())
        groups = defaultdict(list)
        for sym, why, ts in waits:
            groups[(sym, _reason(why))].append(_aware(ts))
        ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:10]
        for (sym, why), ts_list in ranked:
            gaps = [(b - a).total_seconds() / 60.0 for a, b in zip(ts_list, ts_list[1:])]
            small = f"{min(gaps):.1f} min" if gaps else "-"
            print(f"   {len(ts_list):>4} rows  smallest gap {small:>9}  {sym:<12} {why}")
        if not ranked:
            print("   (no WAIT rows in the window)")
        repeats = sum(1 for ts_list in groups.values() for a, b in zip(ts_list, ts_list[1:]) if (b - a).total_seconds() / 60.0 < DEDUPE_MIN * 0.9)
        print(f"\n3) same symbol + reason written again inside {DEDUPE_MIN * 0.9:.1f} min: {repeats}  (0 or near 0 = dedupe is working;")
        print("   a count in the hundreds = it is not. Rows written before the last deploy can still show up: re-run after 30 quiet minutes.)")


main()
