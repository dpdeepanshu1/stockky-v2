#!/usr/bin/env bash
# E565 (round 540): the quick VM check in ONE command (about 30 seconds; the long suite stays scripts/vm_full_check.sh).
#   cd ~/stockky-v2 && bash scripts/vm_quick_check.sh
#   SITE=https://other.example bash scripts/vm_quick_check.sh        # another site name
# Checks: containers, the live site (entry file, cache header, every lazy chunk), gateway tiles and sparklines, runner health, recent log errors,
# git state (clean tracked files, pages.css gone, manifest) and whether today's agent file exists. Exit 0 = no FAIL (WARN and INFO do not fail).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT" || exit 2
SITE="${SITE:-https://stockky.duckdns.org}"; GW="${GW:-http://localhost:8000}"; RUNNER="${RUNNER:-http://localhost:8012}"
PASS=0; FAIL=0; WARN=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/        | /'; }
warn() { WARN=$((WARN+1)); echo "  WARN  $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/        | /'; }
info() { echo "  INFO  $1"; }

echo "== 1. containers"
BAD=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -v -E ' Up ' ; docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -E 'unhealthy|starting|Restarting')
N=$(docker compose ps --format '{{.Name}}' 2>/dev/null | wc -l)
if [ "$N" -eq 0 ]; then bad "docker compose ps lists nothing (is Docker up? are you in ~/stockky-v2?)"
elif [ -z "$BAD" ]; then ok "all $N containers Up and healthy"; else bad "containers not Up / healthy" "$BAD"; fi

echo "== 2. live site and lazy chunks"
JS=$(curl -s --max-time 15 "$SITE/" | grep -o '/assets/index-[^"]*\.js' | head -1)
if [ -z "$JS" ]; then bad "no entry script found in $SITE/ (site down or proxy error)"; else
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$SITE$JS")" = "200" ] && ok "entry $JS is 200" || bad "entry $JS is not 200"
  curl -sI --max-time 15 "$SITE/" | grep -qi '^cache-control:.*no-cache' && ok "index.html is no-cache" || bad "index.html is cacheable (a stale tab would keep old chunk names)"
  MISSING=""; COUNT=0
  for f in $(curl -s --max-time 20 "$SITE$JS" | grep -o 'assets/[A-Za-z0-9_.-]*\.js' | sort -u); do
    COUNT=$((COUNT+1)); c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$SITE/$f"); [ "$c" = "200" ] || MISSING="$MISSING $f($c)"
  done
  [ -z "$MISSING" ] && ok "all $COUNT chunks named by the entry file are 200" || bad "chunks missing:$MISSING"
fi

echo "== 3. gateway: index tiles and sparklines"
IDX=$(curl -s --max-time 15 "$GW/market/indices")
echo "$IDX" | python3 -c '
import json,sys
d=json.load(sys.stdin); bad=[]
for k in ("nifty","sensex","banknifty","vix"):
    t=d.get(k)
    if not isinstance(t,dict) or t.get("price") in (None,0): bad.append(k+" missing")
for k in ("banknifty","vix"):
    t=d.get(k) or {}
    if t.get("change")==0 and t.get("change_pct")==0: bad.append(k+" change is 0.0 (E563 bug)")
print("; ".join(bad)); sys.exit(1 if bad else 0)' > /tmp/qc_idx.txt 2>&1 && ok "indices: nifty, sensex, banknifty, vix present, change not 0.0" || bad "indices" "$(cat /tmp/qc_idx.txt)"
SPK=$(curl -s --max-time 20 "$GW/market/sparklines?symbols=TCS,INFY,BADNAME")
echo "$SPK" | python3 -c '
import json,sys
d=json.load(sys.stdin); s=d.get("series",{})
assert len(s.get("TCS",[]))>=5 and len(s.get("INFY",[]))>=5, "TCS/INFY series short"
assert "BADNAME" in d.get("missing",[]), "BADNAME not reported missing"' >/tmp/qc_spk.txt 2>&1 && ok "sparklines: TCS, INFY fine, BADNAME listed as missing" || bad "sparklines" "$(cat /tmp/qc_spk.txt)"

echo "== 4. runner"
curl -s --max-time 10 "$RUNNER/health" | grep -q '"status":"ok"' && ok "calibration runner health ok" || bad "runner health not ok"

echo "== 5. recent log errors (10 min)"
LOGS=$(docker compose logs --since 10m api-gateway notification-scheduler-service calibration-runner-service frontend 2>&1 | grep -i -E 'traceback|error' | grep -v -i -E 'errors=0|0 errors|no errors' | tail -8)
[ -z "$LOGS" ] && ok "no error or traceback lines" || warn "log lines to read" "$LOGS"

echo "== 6. git"
DIRTY=$(git status --short | grep -v '^??')
[ -z "$DIRTY" ] && ok "no uncommitted tracked changes" || warn "uncommitted tracked changes" "$DIRTY"
[ -z "$(git ls-files frontend/src/styles/pages.css)" ] && [ ! -e frontend/src/styles/pages.css ] && ok "pages.css is gone" || bad "pages.css still exists (git rm frontend/src/styles/pages.css)"
python3 scripts/check_stale_files.py --verify 2>&1 | grep -q 'manifest matches the disk' && ok "file manifest matches the disk" || bad "manifest differs (python3 scripts/check_stale_files.py)"
git fetch -q origin 2>/dev/null; B=$(git rev-parse --abbrev-ref HEAD)
H=$(git rev-parse HEAD 2>/dev/null); O=$(git rev-parse "origin/$B" 2>/dev/null)
[ -n "$H" ] && [ "$H" = "$O" ] && ok "pushed: HEAD equals origin/$B" || warn "HEAD differs from origin/$B (git push, or git pull)"

echo "== 7. agent round 3"
IST_DATE=$(TZ=Asia/Kolkata date +%F); IST_HM=$(TZ=Asia/Kolkata date +%H%M); IST_DOW=$(TZ=Asia/Kolkata date +%u)
F=$(docker compose exec -T calibration-runner-service sh -c "find / -path '*agent/$IST_DATE.json' 2>/dev/null | head -1" 2>/dev/null | tr -d '\r')
MODE=$(docker compose exec -T calibration-runner-service printenv AGENT_MODE 2>/dev/null | tr -d '\r')
info "IST now $IST_DATE $IST_HM, AGENT_MODE in the container: ${MODE:-unknown}"
if [ -n "$F" ]; then ok "agent file for $IST_DATE exists (bash scripts/agent_round3.sh to read it)"
elif [ "$IST_DOW" -ge 6 ] || [ "$IST_HM" -lt 1545 ]; then info "no agent file for $IST_DATE yet (weekend or before 15:45 IST: the agent is refused in market hours; nothing to do)"
else warn "no agent file for $IST_DATE: use the runner page -> run agent now"; fi

echo
echo "SUMMARY: $PASS pass, $WARN warn, $FAIL fail"
[ "$FAIL" -eq 0 ] && { echo "QUICK CHECK OK (run scripts/vm_full_check.sh for the long suite)"; exit 0; } || { echo "QUICK CHECK FAILED"; exit 1; }
