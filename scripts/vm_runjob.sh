#!/usr/bin/env bash
# scripts/vm_runjob.sh - E121: start a calibration-runner job or pack from the VM shell, wait for it, print its output, and say WHY when it does not start.
#
#   source scripts/vm_runjob.sh            # asks for the admin password once, defines login + runjob + runstate
#   runjob '{"kind":"job","target":"wiring_check","params":{}}'
#   runjob '{"kind":"pack","target":"monday_open","params":{}}'
#   runstate                               # what the runner is doing now, and what is waiting
#   runcancel <run-id>                     # cancel a queued or running run
#
# The runner has ONE worker: a run stays "queued" while another run is executing (or a queued one is ahead of it). The older helper just waited 10 minutes and printed an
# empty tail. runjob now polls GET /runs/<id> (the field is "status": queued | running | success | attention | failed | timeout | blocked | cancelled), prints the run
# that is blocking it after 30 s, and stops waiting after RUNJOB_MAX_S (default 900 s) without cancelling anything.
RT=${RT:-http://127.0.0.1:8005}; RN=${RN:-http://127.0.0.1:8012}

runlogin() {
  local p; read -rsp "Admin password: " p; echo
  TOKEN=$(curl -s -X POST "$RT/auth/login" -H 'Content-Type: application/json' -d "{\"username\":\"${ADMIN_USER:-admin}\",\"password\":\"$p\"}" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")
  A="Authorization: Bearer $TOKEN"; echo "token length: ${#TOKEN}"
}

_runget() { curl -s "$RN$1" -H "$A"; }

runstate() {
  _runget "/runs?limit=8" | python3 -c '
import sys, json
d = json.load(sys.stdin); s = d.get("state") or {}
cur = s.get("current")
print("now (IST):", s.get("now_ist"), "| market open:", s.get("market_open_now"))
print("running  :", (cur or {}).get("id"), (cur or {}).get("status"), "since", (cur or {}).get("started_at")) if cur else print("running  : nothing")
for q in s.get("queued") or []: print("queued   :", q.get("id"), q.get("title"))
print("recent runs:")
for r in d.get("runs") or []: print("  ", r.get("id"), r.get("status"), (r.get("exit_summary") or r.get("note") or "")[:80])'
}

runcancel() { curl -s -X POST "$RN/runs/$1/cancel" -H "$A"; echo; }

runjob() {
  local R ID ST max=${RUNJOB_MAX_S:-900} t0=$SECONDS note_at=30
  R=$(curl -s -X POST "$RN/runs" -H "$A" -H 'Content-Type: application/json' -d "$1")
  ID=$(echo "$R" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('id') or '')" 2>/dev/null)
  if [ -z "$ID" ]; then echo "not started: $R" | head -c 600; echo; return 1; fi
  echo "run $ID"
  while :; do
    ST=$(_runget "/runs/$ID" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    case "$ST" in queued|running|"") ;; *) break;; esac
    if [ "$ST" = queued ] && [ $((SECONDS - t0)) -ge "$note_at" ]; then echo "-- still queued after $((SECONDS - t0)) s; the runner is busy with:"; runstate; note_at=$((note_at + 120)); fi
    [ $((SECONDS - t0)) -ge "$max" ] && { echo "-- gave up waiting after ${max}s (status $ST). The run is NOT cancelled: runstate / runcancel $ID"; break; }
    sleep 5
  done
  echo "status: $ST"
  _runget "/runs/$ID/tail?offset=0" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('text') or '(no output)')"
}

# E126: is the timetable on, and which slots will fire today? `schedon` switches it on (a schedule.json saved earlier with enabled=false overrides the new default).
schedule() {
  _runget /schedule | python3 -c '
import sys, json
d = json.load(sys.stdin)
print("timetable:", "ON" if d.get("enabled") else "OFF")
for e in d.get("entries") or []:
    print("  %-16s %-5s %-22s days=%s %s" % (e["id"], e["time"], e["pack"], e["days"], "on" if e.get("enabled") else "off"))
ls = d.get("last_skipped")
print("last skipped slot:", ls if ls else "none")'
}
schedon() { curl -s -X PUT "$RN/schedule" -H "$A" -H 'Content-Type: application/json' -d '{"enabled":true}' >/dev/null; schedule; }
