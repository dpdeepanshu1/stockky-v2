"""E272: label every candidate of one day with +5/+15/+30/+60 minute outcomes and save them beside bar-records.

Run INSIDE the position-stocks container, from ~/stockky-v2 (default day = today IST; run after 15:30):
    docker compose exec -T position-stocks-service python - --day 2026-11-02 < scripts/candidate_outcomes_job.py
Read-only on the database; writes /data/bars/outcomes/<day>.json. Prints the counts (rows, labelled, no_bars, deduped).
"""
import argparse
from datetime import datetime, timedelta, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--day", default=(datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d"))
ap.add_argument("--bars-dir", default="/data/bars")
args = ap.parse_args()

import db        # noqa: E402
import models    # noqa: E402
from orders import candidate_outcomes as co  # noqa: E402

S = db.get_session_factory()()
print(co.run_day(co.rows_from_log(S, models, args.day), args.bars_dir, args.day))
