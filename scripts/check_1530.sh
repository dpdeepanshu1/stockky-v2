#!/usr/bin/env bash
# Round 659 (I5 of the 20 Nov plan): one read-only check for 15:30 IST. Run from ~/stockky-v2:  bash scripts/check_1530.sh
# Prints: gate state, Dhan token hours left, CPU throttle ratio per service, DPY-4024 timeouts per service with the UTC minute,
# today's WAIT / ENTER row counts, write-behind + WAIT-dedupe counters, last log-retention line, F&O overview head.
# Round 676 adds the /ops/db-latency summary and the open-window 'oracle slow' count (P0.3 of the 23 Nov plan).
# Round 667 adds I1 (compose caps), D4 (entry SLOW lines) and D2 (ASH wait events) at the end, so one paste unblocks them.
# Asks for the admin username / password (hidden, never printed or stored). Changes nothing.
set -u
cd "$(dirname "$0")/.." || exit 1
RT_URL="${REAL_TRADE_URL:-http://127.0.0.1:8005}"
FNO_URL="${FNO_URL:-http://127.0.0.1:8007}"
DAY="${DAY:-$(TZ=Asia/Kolkata date +%F)}"
# round 678: a DAY after today has no rows yet (23 Nov: DAY=2026-11-24 was typed on 23 Nov and every "today" part came back empty). Say so instead of printing zeros.
if [[ "$DAY" > "$(TZ=Asia/Kolkata date +%F)" ]]; then
  echo "WARNING: DAY=$DAY is after today ($(TZ=Asia/Kolkata date +%F) IST): that day has no rows yet, so counts of 0 below mean nothing. Run it on the day itself (leave DAY unset)." >&2
fi
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
get() { # $1 = url ; prints compact JSON or the error
  curl -s -m 20 -H "Authorization: Bearer ${TOKEN:-}" "$1" | python3 -c 'import sys,json
try: print(json.dumps(json.load(sys.stdin))[:1500])
except Exception as e: print("unreadable answer:", e)'
}
echo "== $(TZ=Asia/Kolkata date '+%F %H:%M') IST  (day $DAY) =="
[ -z "$TOKEN" ] && echo "login failed: the gate, token and counter sections are skipped (the container checks below still run)"
if [ -n "$TOKEN" ]; then
  echo; echo "== REAL gate (/status/REAL): expect armed, disarmed_reason null =="; get "$RT_URL/status/REAL"
  echo; echo "== Dhan token (/dhan/status): hours remaining =="; get "$RT_URL/dhan/status"
  echo; echo "== write-behind + WAIT dedupe counters =="; get "$RT_URL/write-behind/status"
fi
echo; echo "== CPU throttle, NOW: share of 100 ms periods that hit the cpu cap during a 20 s sample (since-start ratio below it) =="
_thr() { docker compose exec -T "$1" sh -c 'echo $(grep nr_periods /sys/fs/cgroup/cpu.stat | cut -d" " -f2) $(grep nr_throttled /sys/fs/cgroup/cpu.stat | cut -d" " -f2)' 2>/dev/null; }
declare -A _P0 _T0
for c in position-stocks-service real-trade-service market-data-service; do read -r _P0[$c] _T0[$c] < <(_thr "$c"); done
sleep 20
for c in position-stocks-service real-trade-service market-data-service; do
  read -r p1 t1 < <(_thr "$c")
  dp=$(( ${p1:-0} - ${_P0[$c]:-0} )); dt=$(( ${t1:-0} - ${_T0[$c]:-0} ))
  [ "$dp" -gt 0 ] && echo "$c: $dt of $dp periods throttled in 20 s ($(( 100 * dt / dp )) %)" || echo "$c: not readable"
done
echo; echo "== since-start CPU throttle (since container start): throttled / periods =="
for c in position-stocks-service real-trade-service market-data-service; do
  docker compose exec -T "$c" sh -c 'p=$(grep nr_periods /sys/fs/cgroup/cpu.stat | cut -d" " -f2); t=$(grep nr_throttled /sys/fs/cgroup/cpu.stat | cut -d" " -f2); echo "'"$c"' throttled=$t periods=$p $(awk -v t="$t" -v p="$p" "BEGIN{ if (p>0) printf \"(%.1f%%)\", 100*t/p }")"' 2>/dev/null || echo "$c: not readable"
done
echo; echo "== DPY-4024 call timeouts in the last 24 h, per service (target: 2 or fewer per market day) =="
for s in real-trade-service position-stocks-service market-data-service; do
  echo -n "$s total: "; docker compose logs --since 24h --no-color "$s" 2>/dev/null | grep -c DPY-4024
done
echo "-- per minute, UTC (add 5:30 for IST); the same minute in two services points at the database"
for s in position-stocks-service real-trade-service; do
  echo "$s"; docker compose logs --since 24h --timestamps --no-color "$s" 2>/dev/null | grep DPY-4024 | grep -o -E '20[0-9-]+T[0-9:]{5}' | sort | uniq -c
done
echo; echo "== decisions today, REAL entry rows by action (WAIT target: under about 300 a day) =="
docker compose exec -T -e DAY="$DAY" real-trade-service python3 - <<'E' 2>&1 | tail -15
import os
from datetime import datetime, timedelta
from db import get_session_factory
from sqlalchemy import MetaData, Table, select, func
d = datetime.strptime(os.environ["DAY"], "%Y-%m-%d")
s = get_session_factory()()
t = Table('trade_decisions', MetaData(), autoload_with=s.get_bind())
q = (select(t.c['action'], func.count()).where(t.c['mode'] == 'REAL', t.c['decision_type'] == 'ENTRY',
     t.c['created_at'] >= d - timedelta(hours=5, minutes=30), t.c['created_at'] < d + timedelta(hours=18, minutes=30)).group_by(t.c['action']))
for r in s.execute(q): print(tuple(r))
E

# round 679: 23 Nov showed 1,466 REAL entry WAIT rows against a target under about 300, and the 3,641-of-5,553 issue list left 1,912 WAITs without a gate. Read-only: which gate_tag
# the WAIT rows carry, how many different names that is, and the names that wrote the most rows (the same name repeating = what a WAIT dedupe could drop).
echo; echo "== REAL entry WAIT rows by gate_tag: rows / different names (top 12), then the 8 names with most rows =="
docker compose exec -T -e DAY="$DAY" real-trade-service python3 - <<'E' 2>&1 | tail -24
import os
from datetime import datetime, timedelta
from db import get_session_factory
from sqlalchemy import MetaData, Table, select, func
d = datetime.strptime(os.environ["DAY"], "%Y-%m-%d")
s = get_session_factory()()
t = Table('trade_decisions', MetaData(), autoload_with=s.get_bind())
w = [t.c['mode'] == 'REAL', t.c['decision_type'] == 'ENTRY', t.c['action'] == 'WAIT',
     t.c['created_at'] >= d - timedelta(hours=5, minutes=30), t.c['created_at'] < d + timedelta(hours=18, minutes=30)]
q = select(t.c['gate_tag'], func.count(), func.count(func.distinct(t.c['symbol']))).where(*w).group_by(t.c['gate_tag']).order_by(func.count().desc())
for i, r in enumerate(s.execute(q)):
    if i < 12: print(f"  {str(r[0]):<28} rows {r[1]:>5}   names {r[2]:>4}")
q2 = select(t.c['symbol'], func.count()).where(*w).group_by(t.c['symbol']).order_by(func.count().desc())
print("  most rows:", ", ".join(f"{r[0]} {r[1]}" for i, r in enumerate(s.execute(q2)) if i < 8))
E
echo; echo "== Oracle latency since each container start (connect / execute: count, slow count, p95, max ms; slow = ORACLE_SLOW_QUERY_MS, 1500) =="
PS_URL="${POSITION_STOCKS_URL:-http://127.0.0.1:8006}"
for u in "real-trade:$RT_URL" "position-stocks:$PS_URL"; do
  echo "-- ${u%%:*}"
  curl -s -m 20 "${u#*:}/ops/db-latency" | python3 -c 'import sys,json
try:
    d = json.load(sys.stdin)
    for lab, e in (d.get("engines") or {}).items():
        x, c = e.get("exec") or {}, e.get("connect") or {}
        print("  %-22s exec n=%s slow=%s p95=%s max=%s | connect n=%s slow=%s p95=%s max=%s" % (lab, x.get("n"), x.get("slow_n"), x.get("p95_ms"), x.get("max_ms"), c.get("n"), c.get("slow_n"), c.get("p95_ms"), c.get("max_ms")))
        if e.get("last_slow"): print("    last slow:", json.dumps(e["last_slow"])[:300])
    if not d.get("engines"): print("  no engines in the answer:", json.dumps(d)[:300])
except Exception as ex: print("  unreadable answer:", ex)'
done
echo; echo "== 'oracle slow' warnings in the open window of $DAY (09:00-11:00 IST = 03:30-05:30 UTC; 0 on a day that has not reached 11:00 proves nothing) =="
for s in real-trade-service position-stocks-service; do
  echo -n "$s: "; docker compose logs --since "${DAY}T03:30:00Z" --until "${DAY}T05:30:00Z" --no-color "$s" 2>/dev/null | grep -c -i "oracle slow"
done
echo; echo "== log retention (last lines) =="
docker compose logs --since 48h --no-color real-trade-service 2>/dev/null | grep "log-retention" | tail -3
echo; echo "== F&O overview (head) =="
curl -s -m 20 -H "Authorization: Bearer ${TOKEN:-}" "$FNO_URL/fno/overview" | head -c 800; echo

# ---- Round 667: what the waiting items need, in the same paste (read-only) ----
echo; echo "== I1: compose caps (expect position-stocks cpus=700000000, market-data mem=1677721600) =="
docker inspect -f '{{.Name}} cpus={{.HostConfig.NanoCpus}} mem={{.HostConfig.Memory}}' $(docker ps -q) 2>/dev/null | sort
echo; echo "== D4: slowest entry attempts, last 24 h (position-stocks 'entry SLOW' lines, newest 25) =="
docker compose logs --since 24h --no-color position-stocks-service 2>/dev/null | grep "entry SLOW" | tail -25 | cut -c1-400
echo; echo "== D2: what the DB waited on (ASH, last 3 h; if this errors, use the OCI Metrics page instead) =="
docker compose exec -T real-trade-service python3 - <<'E' 2>&1 | tail -60
import sys; sys.path.insert(0, "/app")
import db
from sqlalchemy import text
try:
    with db.get_engine().connect() as c:
        print("TOP WAIT EVENTS, last 3 hours")
        for r in c.execute(text("select nvl(event,'ON CPU') ev, count(*) n from v$active_session_history where sample_time > systimestamp - interval '3' hour group by nvl(event,'ON CPU') order by n desc fetch first 12 rows only")):
            print(f"{r[1]:>8} {r[0]}")
        print("\nSAMPLES PER MINUTE, last 30 minutes")
        for r in c.execute(text("select to_char(sample_time,'HH24:MI') m, count(*) n from v$active_session_history where sample_time > systimestamp - interval '30' minute group by to_char(sample_time,'HH24:MI') order by 1")):
            print(r[0], r[1])
except Exception as e:
    print("ASH not readable:", str(e)[:200])
E
