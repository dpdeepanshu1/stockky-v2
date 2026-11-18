#!/usr/bin/env bash
# Round 598: one command for the after-close read (15:45 IST or later). Read-only. Run from ~/stockky-v2:
#     bash scripts/eod_check.sh            # today
#     DAY=2026-11-17 bash scripts/eod_check.sh
# Asks for the admin username / password (password hidden, never printed or stored), logs in on real-trade (POST /auth/login), then runs:
#   guard_log_summary (decisions tables + container logs), daily_trade_report (Real Trade included), today's gate counts, and blocked_outcome for no_setup_window.
set -u
cd "$(dirname "$0")/.." || exit 1
DAY="${DAY:-$(TZ=Asia/Kolkata date +%F)}"
RT_URL="${REAL_TRADE_URL:-http://127.0.0.1:8005}"
read -r -p "admin username [admin]: " ADM_U
read -r -s -p "admin password: " ADM_P; echo
TOKEN=$(ADM_U="${ADM_U:-admin}" ADM_P="$ADM_P" RT_URL="$RT_URL" python3 -c '
import json, os, urllib.request
req = urllib.request.Request(os.environ["RT_URL"] + "/auth/login",
      data=json.dumps({"username": os.environ["ADM_U"], "password": os.environ["ADM_P"]}).encode(),
      headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=30)).get("token", ""))
' 2>/dev/null)
unset ADM_P
if [ -z "$TOKEN" ]; then echo "login failed (wrong username / password, or real-trade not running). Nothing else was run."; exit 1; fi
export TOKEN
echo "== guard_log_summary =="; python3 scripts/guard_log_summary.py --since 12h
echo; echo "== daily_trade_report =="; python3 scripts/daily_trade_report.py --days 2 --origin all --out daily_trades.txt
echo; echo "== gate counts, $DAY (REAL entry decisions) =="
docker compose exec -T -e DAY="$DAY" real-trade-service python3 - <<'E'
import os
from datetime import datetime, timedelta
from db import get_session_factory
from sqlalchemy import MetaData, Table, select, func, desc
d = datetime.strptime(os.environ["DAY"], "%Y-%m-%d")
s = get_session_factory()()
t = Table('trade_decisions', MetaData(), autoload_with=s.get_bind())
q = (select(t.c['gate_tag'], t.c['action'], func.count().label('c'))
     .where(t.c['mode'] == 'REAL', t.c['decision_type'] == 'ENTRY',
            t.c['created_at'] >= d + timedelta(hours=3, minutes=45), t.c['created_at'] < d + timedelta(hours=10))
     .group_by(t.c['gate_tag'], t.c['action']).order_by(desc('c')))
for r in s.execute(q):
    print(tuple(r))
E
echo; echo "== blocked_outcome (no_setup_window) =="
docker compose exec -T real-trade-service python - --day "$DAY" < scripts/blocked_outcome.py
echo; echo "== OR_SHADOW rule A (soft_stack + matched setup) =="
docker compose exec -T real-trade-service python - --day "$DAY" --gate soft_stack --shadow A < scripts/blocked_outcome.py
echo; echo "== OR_SHADOW rule B (no_setup_window + typed event + above VWAP + sector top third) =="
docker compose exec -T real-trade-service python - --day "$DAY" --gate no_setup_window --shadow B < scripts/blocked_outcome.py
echo; echo "== OR_SHADOW log lines (all day) =="
docker compose logs --since 14h real-trade-service 2>/dev/null | grep "OR_SHADOW" | sort -u | tail -20
