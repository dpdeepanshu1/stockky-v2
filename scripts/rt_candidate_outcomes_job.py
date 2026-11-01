"""E272 round 8: label every Real Trade ENTRY decision of one day with +5/+15/+30/+60 minute outcomes (from position-stocks' recorded bars).

Run INSIDE the real-trade container, from ~/stockky-v2 (default day = today IST; run after 15:30):
    docker compose exec -T real-trade-service python - --day 2026-11-02 < scripts/rt_candidate_outcomes_job.py
Read-only on the database; writes /data/bars/rt/outcomes/<day>.json only. Prints the counts (rows, labelled, no_bars, deduped).
"""
import argparse
from datetime import datetime, timedelta, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--day", default=(datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d"))
ap.add_argument("--bars-dir", default="/data/bars")
ap.add_argument("--rt-dir", default="/data/bars/rt")
args = ap.parse_args()

from db import get_session_factory  # noqa: E402
import models                       # noqa: E402
from entry_engine import candidate_outcomes as co  # noqa: E402

S = get_session_factory()()
print(co.run_day_rt(co.rows_from_decisions(S, models, args.day), args.bars_dir, args.rt_dir, args.day))
