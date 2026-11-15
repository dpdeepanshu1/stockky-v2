#!/usr/bin/env bash
# E565 (round 540): the quick VM check in ONE command (about 30 seconds; the long suite stays scripts/vm_full_check.sh).
#   cd ~/stockky-v2 && bash scripts/vm_quick_check.sh
#   SITE=https://other.example bash scripts/vm_quick_check.sh        # another site name
# Checks: containers, the live site (entry file, cache header, every lazy chunk), gateway tiles and sparklines, runner health, recent log errors,
# git state (clean tracked files, pages.css gone, manifest) and the agent's daily proof (did it run today: the runner's /agent/loop with ADMIN_TOKEN set, else its run log). Exit 0 = no FAIL (WARN and INFO do not fail).
#   ADMIN_TOKEN=<admin session token> bash scripts/vm_quick_check.sh        # also prints tonight's chain from /agent/loop
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT" || exit 2
SITE="${SITE:-https://stockky.duckdns.org}"; GW="${GW:-http://localhost:8000}"; RUNNER="${RUNNER:-http://localhost:8012}"
PASS=0; FAIL=0; WARN=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/        | /'; }
warn() { WARN=$((WARN+1)); echo "  WARN  $1"; [ -n "${2:-}" ] && echo "$2" | sed 's/^/        | /'; }
info() { echo "  INFO  $1"; }

# E586 (round 561): right after `docker compose up` containers read "health: starting" for up to a minute, which made check 1 (and the gateway checks) FAIL for a healthy stack.
# Wait until none is starting / unhealthy / restarting (QC_WAIT_S seconds, default 90; QC_WAIT_S=0 skips the wait). A container that is still not healthy after that is a real FAIL.
QC_WAIT_S="${QC_WAIT_S:-90}"; _t0=$SECONDS
while [ $((SECONDS-_t0)) -lt "$QC_WAIT_S" ] && docker compose ps --format '{{.Status}}' 2>/dev/null | grep -qE 'starting|unhealthy|Restarting'; do sleep 3; done
[ $((SECONDS-_t0)) -ge 4 ] && echo "  (waited $((SECONDS-_t0)) s for containers to settle)"

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
GCU=$(curl -s --max-time 40 "$GW/market/global-cues")
echo "$GCU" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("rows",{}); m=d.get("missing",[])
assert len(r)+len(m)==6, "rows + missing is not 6"
for k,v in r.items():
    assert v.get("price",0)>0 and v.get("bar_date"), k+" row has no price or no date"
print(str(len(r))+" of 6 cues read" + (", missing: "+",".join(m) if m else ""))' >/tmp/qc_gcu.txt 2>&1 && ok "global cues (round 553): $(cat /tmp/qc_gcu.txt)" || bad "global cues" "$(cat /tmp/qc_gcu.txt)"
FEOD=$(curl -s --max-time 90 "$GW/market/fo-eod")
echo "$FEOD" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("rows",[]); td=d.get("trade_date")
assert td and len(td)==10, "no trade date on the pack: "+str(d.get("error"))
assert len(r)>=100, "only "+str(len(r))+" F&O stocks in the pack (expected about 180-220)"
c=d.get("counts",{})
assert sum(c.get(k,0) for k in ("long_buildup","short_covering","short_buildup","long_unwinding"))>0, "no build-up classified at all"
nb=sum(1 for x in r if x.get("basis_pct") is None)
assert nb<len(r)*0.5, "basis missing for "+str(nb)+" of "+str(len(r))+" stocks (cash price column?)"
print("close of "+td+(" (STALE, expected "+str(d.get("expected_date"))+")" if d.get("stale") else "")+", "+str(len(r))+" stocks, "+str(nb)+" without basis")' >/tmp/qc_feod.txt 2>&1 && ok "F&O end-of-day pack (round 554): $(cat /tmp/qc_feod.txt)" || bad "F&O end-of-day pack" "$(cat /tmp/qc_feod.txt)"

# E586 (round 561): rounds 559-560 wiring on the live stack. Bank Nifty is served by market-data (:8001) and the F&O service (:8007), NOT by the gateway (:8000), and the stock
# popup's liquidity badge by position-stocks (:8006). WARN, not FAIL: they depend on Dhan / the session. The two token checks run only with ADMIN_TOKEN set.
MD="${MD:-http://localhost:8001}"; FNO="${FNO:-http://localhost:8007}"; PS="${PS:-http://localhost:8006}"
BNX=$(curl -s --max-time 20 "$MD/dhan/options/expiries?underlying=BANKNIFTY")
echo "$BNX" | python3 -c '
import json,sys
d=json.load(sys.stdin); e=d.get("expiries") if isinstance(d,dict) else None
assert e, "no expiry list: "+str(d)[:160]
print(str(len(e))+" expiries, nearest "+str(e[0]))' >/tmp/qc_bnx.txt 2>&1 && ok "Bank Nifty view only (round 559): $(cat /tmp/qc_bnx.txt)" || warn "Bank Nifty expiries from market-data" "$(tail -2 /tmp/qc_bnx.txt)"
if [ -n "${ADMIN_TOKEN:-}" ]; then
  curl -s --max-time 20 "$FNO/fno/overview" -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -c '
import json,sys
d=json.load(sys.stdin); u=d.get("underlyings")
assert u is not None, "no underlyings key: "+str(d)[:160]
assert sorted(u)==["NIFTY","SENSEX"], "the engine lists "+str(u)+" (Bank Nifty must stay view only)"
print("engine lists "+",".join(u))' >/tmp/qc_fnou.txt 2>&1 && ok "F&O engine underlyings (round 559): $(cat /tmp/qc_fnou.txt)" || warn "F&O engine underlyings" "$(tail -2 /tmp/qc_fnou.txt)"
  curl -s --max-time 25 "$PS/board/RELIANCE" -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -c '
import json,sys
d=json.load(sys.stdin); assert "liquidity" in d, "no liquidity key on /board/RELIANCE: "+str(d)[:160]
l=d["liquidity"] or {}; print("key present, fields: "+",".join(sorted(l)) if isinstance(l,dict) else "key present")' >/tmp/qc_liq.txt 2>&1 && ok "popup liquidity badge (round 559): $(cat /tmp/qc_liq.txt)" || warn "popup liquidity badge" "$(tail -2 /tmp/qc_liq.txt)"
else
  info "set ADMIN_TOKEN to also check the F&O engine list and the popup liquidity key"
fi

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

echo "== 7. agent daily proof (round 544)"
IST_DATE=$(TZ=Asia/Kolkata date +%F); IST_HM=$(TZ=Asia/Kolkata date +%H%M); IST_DOW=$(TZ=Asia/Kolkata date +%u)
SVC=calibration-runner-service
MODE=$(docker compose exec -T $SVC printenv AGENT_MODE 2>/dev/null | tr -d '\r')
info "IST now $IST_DATE $IST_HM, AGENT_MODE in the container: ${MODE:-unknown} (the saved setting on the page wins over .env)"
LOOP_DONE=0
if [ -n "${ADMIN_TOKEN:-}" ]; then
  # preferred: the runner's own view of the whole loop (needs the admin token: ADMIN_TOKEN=<token> bash scripts/vm_quick_check.sh)
  LOOP=$(curl -s --max-time 20 -H "Authorization: Bearer $ADMIN_TOKEN" "$RUNNER/agent/loop")
  OUT=$(printf '%s' "$LOOP" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("the reply is not JSON (wrong token, or the runner is the old one)"); sys.exit(3)
p=(d.get("agent") or {}).get("proof")
if not isinstance(p,dict): print("no proof field: the runner is older than round 544"); sys.exit(3)
print("agent proof: %s - %s (warns after %s IST, %s run(s) today)" % (p.get("state"),p.get("detail"),p.get("warn_after_ist"),p.get("runs_today")))
for c in d.get("chain") or []: print("  %-8s %s - %s" % (c.get("state"),c.get("title"),c.get("detail")))
sys.exit(2 if p.get("state")=="missed" else 0)' 2>&1); RC=$?
  if [ $RC -eq 0 ]; then ok "agent proof from /agent/loop"; echo "$OUT" | sed 's/^/        | /'; LOOP_DONE=1
  elif [ $RC -eq 2 ]; then warn "the agent did not run today (the runner sends one Telegram warning after 17:30 IST)" "$OUT"; LOOP_DONE=1
  else warn "/agent/loop could not be read (using the run log instead)" "$OUT"; fi
fi
if [ $LOOP_DONE -eq 0 ]; then
  # no token (or the loop could not be read): the run log and the day file inside the container
  RL=$(docker compose exec -T $SVC sh -c "find / -path '*agent/runs.jsonl' 2>/dev/null | head -1" 2>/dev/null | tr -d '\r')
  F=$(docker compose exec -T $SVC sh -c "find / -path '*agent/$IST_DATE.json' 2>/dev/null | head -1" 2>/dev/null | tr -d '\r')
  if [ -n "$RL" ]; then
    SUM=$(docker compose exec -T $SVC cat "$RL" 2>/dev/null | python3 -c '
import json,sys
day=sys.argv[1]; runs=[]
for x in sys.stdin.read().splitlines():
    try: d=json.loads(x)
    except Exception: continue
    if isinstance(d,dict) and d.get("day")==day: runs.append(d)
good=[r for r in runs if r.get("ok",True)]; bad=[r for r in runs if not r.get("ok",True)]
print("%d saved run(s) today (%d scheduled), %d crashed" % (len(good),sum(1 for r in good if r.get("trigger")=="schedule"),len(bad)))
if good: print("last: %s IST %s, %s idea(s), source %s" % (str(good[-1].get("at_ist"))[11:16],good[-1].get("trigger"),good[-1].get("ideas"),good[-1].get("source") or "none"))
if bad: print("crash: %s" % bad[-1].get("error"))
sys.exit(0 if good else 1)' "$IST_DATE" 2>&1); RC=$?
    if [ $RC -eq 0 ]; then ok "agent ran today ($IST_DATE)"; echo "$SUM" | sed 's/^/        | /'
    elif [ "$IST_DOW" -ge 6 ]; then info "no agent run on a weekend"
    elif [ "$IST_HM" -lt 1545 ]; then info "no agent run yet today (before 15:45 IST the agent is not started)"
    elif [ "$IST_HM" -lt 1730 ]; then info "no agent run yet today; it starts after the run-after time (16:10 by default) and is reported missing after 17:30 IST" "$SUM"
    else warn "no agent run saved for $IST_DATE (the runner sends one Telegram warning after 17:30 IST): runner page -> Self Improve System -> Today / Loop -> Run now" "$SUM"; fi
  elif [ -n "$F" ]; then ok "agent file for $IST_DATE exists (round 544 run log not found: the runner is older than round 544)"
  elif [ "$IST_DOW" -ge 6 ] || [ "$IST_HM" -lt 1545 ]; then info "no agent file for $IST_DATE yet (weekend or before 15:45 IST: the agent is refused in market hours; nothing to do)"
  else warn "no agent file for $IST_DATE: use the runner page -> Self Improve System -> Run now"; fi
fi

echo
echo "SUMMARY: $PASS pass, $WARN warn, $FAIL fail"
[ "$FAIL" -eq 0 ] && { echo "QUICK CHECK OK (run scripts/vm_full_check.sh for the long suite)"; exit 0; } || { echo "QUICK CHECK FAILED"; exit 1; }
