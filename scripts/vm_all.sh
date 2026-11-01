#!/usr/bin/env bash
# E285 round 98: EVERY VM command in one file. Run from anywhere on the VM; it cd's to ~/stockky-v2 (or $REPO).
#
#   bash scripts/vm_all.sh apply  ~/stockky-v2-E285-round98.zip   # unpack the release over the checkout (adds / overwrites, never deletes) + stale-file check
#       (this script is already on the VM since round 96; to pick up a NEWER copy of it first: unzip -p <zip> stockky-v2-main/scripts/vm_all.sh > /tmp/vm_all.sh && bash /tmp/vm_all.sh apply <zip>)
#   bash scripts/vm_all.sh deploy                                  # rebuild + restart the three changed services
#   bash scripts/vm_all.sh test                                    # repo checks + the changed service suites in Docker + the frontend check
#   bash scripts/vm_all.sh check                                   # read-only health report -> ~/vm_report.txt (asks for the admin login)
#   bash scripts/vm_all.sh logs [hours]                            # read-only LOG SCAN of every container (default 12 h) -> ~/vm_logs.txt; no login, paste the file back
#   bash scripts/vm_all.sh halfsell status                         # the last half-sell probe result (places nothing; asks for the admin login)
#   bash scripts/vm_all.sh halfsell run [SYMBOL]                   # THE HALF-SELL PROBE: REAL orders (buy 2, sell 1, sell 1), market hours 09:30-14:30 IST, you must watch Dhan
#   bash scripts/vm_all.sh sweep                                   # the offline sweep study (a few minutes; the verdict so far is "neither", nothing ships from it)
#   bash scripts/vm_all.sh all  <zip>                              # apply + deploy + test + check, stopping at the first failing step
#
# Nothing here places an order EXCEPT `halfsell run`, which asks you to type HALFSELL first. `check`, `logs` and `halfsell status` read only (they log in as admin to read GET routes
# where needed; the password is read without echo and is never written to a report).
set -uo pipefail
REPO="${REPO:-$HOME/stockky-v2}"
BASE="${BASE:-https://stockky.duckdns.org}"
REPORT="${REPORT:-$HOME/vm_report.txt}"
cd "$REPO" || { echo "no repo at $REPO (set REPO=...)"; exit 2; }
hr(){ echo; echo "=================== $*"; }

cmd_apply(){
  local zip="${1:-}"; [ -f "$zip" ] || { echo "usage: bash scripts/vm_all.sh apply <release.zip>"; return 2; }
  command -v unzip >/dev/null && command -v tar >/dev/null || { echo "unzip is needed: sudo apt-get install -y unzip"; return 2; }
  local tmp; tmp="$(mktemp -d)"; unzip -q -o "$zip" -d "$tmp" || return 1
  local src="$tmp"; [ "$(ls -1 "$tmp" | wc -l)" = "1" ] && [ -d "$tmp/$(ls -1 "$tmp")" ] && src="$tmp/$(ls -1 "$tmp")"
  hr "apply: copy $zip over $REPO (.env, .git, data/ (the model file) and the Oracle wallet are not touched)"
  ( cd "$src" && tar cf - --exclude='./.git' --exclude='./.env' --exclude='./.env.*.local' --exclude='./oracle_wallet' --exclude='./data' . ) | ( cd "$REPO" && tar xf - ) || return 1
  rm -rf "$tmp"
  hr "apply: repo checks"
  python3 scripts/check_merge_conflicts.py || return 1
  python3 scripts/check_shared_copies.py || return 1
  python3 scripts/gen_env_reference.py --check || return 1
  hr "apply: tracked files the release no longer has (READ the list; each line is a git rm for a file the release dropped. After round 96 this should say 'no stale files')"
  python3 scripts/check_stale_files.py; echo "(exit $? : 0 = nothing stale, 1 = read the git rm lines above before running any)"
  return 0
}

cmd_deploy(){
  hr "deploy: rebuild and restart position-stocks, real-trade and the frontend (the card)"
  docker compose up -d --build position-stocks-service real-trade-service frontend || return 1
  echo "waiting for health..."; for i in $(seq 1 30); do
    n=$(docker compose ps position-stocks-service real-trade-service 2>/dev/null | grep -c "(healthy)"); [ "$n" = "2" ] && break; sleep 5; done
  docker compose ps position-stocks-service real-trade-service frontend
  [ "${n:-0}" = "2" ] || { echo "a service is not healthy yet: docker compose logs --tail 60 position-stocks-service real-trade-service"; return 1; }
}

cmd_test(){
  hr "test: repo checks and tooling tests"
  python3 scripts/check_control_catalog.py || return 1
  python3 -m pytest scripts/test_repo_scripts.py -q || return 1
  hr "test: the changed Position Stocks tests, then both whole suites in Docker (same Python as production)"
  bash scripts/docker_test.sh position-stocks-service tests/test_half_sell_probe.py tests/test_sell_probe.py tests/test_e285_round96_live_state.py tests/test_e283_live_state.py tests/test_e284_volume_profile.py tests/test_e285_ip_guard.py -q || return 1
  bash scripts/docker_test.sh position-stocks-service -q || return 1
  bash scripts/docker_test.sh real-trade-service -q || return 1
  hr "test: frontend (npm ci + tsc + build + unit tests)"
  bash scripts/docker_frontend_check.sh || return 1
}

cmd_check(){
  exec > >(tee "$REPORT") 2>&1
  local U P RT PS
  read -rp "admin user: " U; read -rsp "admin password: " P; echo
  tok(){ curl -s -m 20 -X POST "$BASE/$1/auth/login" -H "Content-Type: application/json" -d "{\"username\":\"$U\",\"password\":\"$P\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null; }
  RT=$(tok realtrade); PS=$(tok positionstocks); unset P
  [ -n "$RT" ] && echo "RT login ok" || echo "RT LOGIN FAILED (token steps will be empty)"
  [ -n "$PS" ] && echo "PS login ok" || echo "PS LOGIN FAILED (token steps will be empty)"
  get(){ curl -s -m 30 -H "Authorization: Bearer $2" "$BASE/$1" | python3 -m json.tool 2>/dev/null | head -"${3:-70}"; }

  hr "1. code version, containers, stale files (manifest must match: 'no stale files')"
  date; git log --oneline -3
  python3 scripts/check_stale_files.py 2>&1 | tail -5
  docker compose ps position-stocks-service real-trade-service frontend

  hr "2. e277 VM check (switches, heat, services)"
  bash scripts/e277_vm_check.sh 2>&1 | grep -v "^git rm" | tail -25

  hr "3. effective config (PS: live live enforce enforce True 8 | RT: enforce enforce enforce True | new switches all True)"
  docker compose exec -T position-stocks-service python -c "import config as c; print('PS', c.SHORT_MODE, c.TRADE_ODDS_MODE, c.STRUCTURE_GATE_MODE, c.STRUCTURE_BREAKOUT_VOLUME_MODE, c.ORDER_RATE_SHARED_ENABLED, c.ORDER_RATE_ACCOUNT_PER_S); print('PS ip/cap', c.IP_GUARD_ENABLED, c.ENTRY_IP_PAUSE_MINUTES, c.DHAN_MODIFY_CAP_ENABLED, c.DHAN_MODIFY_CAP_PER_ORDER, c.DHAN_MODIFY_RESERVE, 'volume', c.VOLUME_PROFILE_LEARNED_ENABLED)" 2>&1 | tail -3
  docker compose exec -T real-trade-service python -c "import config as c; print('RT', c.STRUCTURE_GATE_MODE, c.STRUCTURE_GATE_DEMO_MODE, c.STRUCTURE_BREAKOUT_VOLUME_MODE, c.ORDER_RATE_SHARED_ENABLED); print('RT ladder', c.RISK_LADDER_ENABLED, c.RISK_LADDER_LOSS_DAYS_ENABLED, c.RISK_LADDER_RISK_ENABLED, c.RISK_LADDER_RISK_STEP)" 2>&1 | tail -3

  hr "4. Dhan IP rule: is this host the registered IP? (expect verdict OK; MISMATCH / NOT_CONFIGURED = every order will be refused)"
  get positionstocks/dhan/ip-status "$PS" 40
  echo "VM public IP (compare by hand with the static IP in Dhan Web):"; vm_ip=""; for u in https://api.ipify.org https://checkip.amazonaws.com https://ifconfig.me; do vm_ip="$(curl -s -m 8 "$u" 2>/dev/null | tr -d '[:space:]')"; echo "$vm_ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' && break; vm_ip=""; done; echo "${vm_ip:-could not read it from ipify / amazonaws / ifconfig.me (an outside service was down; the Dhan ip-status above and the network-check below report the IP too)}"
  get realtrade/dhan/network-check "$RT" 25

  hr "5. Position Stocks /live/state (order rate, momentum, heat, ladder, ip_guard, modify_cap, volume_profile)"
  get positionstocks/live/state "$PS" 150

  hr "6. learned volume curve: wait for the background build (up to 90 s), then read it from the RUNNING service (expect ready true, days >= 5)"
  for i in $(seq 1 18); do
    b=$(curl -s -m 20 -H "Authorization: Bearer $PS" "$BASE/positionstocks/live/state" | python3 -c "import sys,json; v=json.load(sys.stdin).get('volume_profile',{}); print(v.get('building'), v.get('ready'), v.get('days'), v.get('symbol_days'), v.get('error'))" 2>/dev/null)
    echo "  try $i: building ready days symbol_days error = $b"; case "$b" in "False "*) break;; esac; sleep 5; done

  hr "7. Real Trade /live/state (pool, heat, setup stamps, order window)"
  get realtrade/live/state "$RT" 90

  hr "8. shared Dhan window file"
  docker compose exec -T position-stocks-service sh -c 'ls -la /data/shared; cat /data/shared/order_window.json 2>/dev/null; echo' 2>&1 | tail -6

  hr "9. news sources and premarket plan (Moneycontrol and NDTVProfit direct feeds are known stale / blocked; the *-google ones are the ones switched on)"
  python3 scripts/check_event_sources.py 2>&1 | tail -14
  get positionstocks/premarket/plan "$PS" 30

  hr "10. Real Trade structure gate (status and recent log lines)"
  get realtrade/structure/status/REAL "$RT" 12
  docker compose logs --since 10h real-trade-service 2>&1 | grep -i structure | grep -v "real-trade-db" | tail -6

  hr "11. today: heat refusals, momentum exits, order-rate waits, IP refusals, swing decisions (database start-up lines are filtered out)"
  docker compose logs --since 10h position-stocks-service real-trade-service 2>&1 | grep -i -E "HEAT_LIMIT|MOMENTUM_EXIT|order rate|source IP|ip guard|modify cap|DhanModifyCap" | tail -20
  docker compose logs --since 10h real-trade-service 2>&1 | grep -i swing | grep -v -E "real-trade-db|attached|added " | tail -10

  hr "12. NO_SETUP readiness (needs trading sessions; NOT_READY = change nothing)"
  TOKEN="$PS" python3 scripts/no_setup_readiness.py --service http://127.0.0.1:8006 2>&1 | tail -12

  hr "13. errors in the last 2 hours (expect nothing)"
  docker compose logs --since 2h position-stocks-service real-trade-service 2>&1 | grep -i -E "traceback|critical|Dhan refused an order call" | tail -10

  hr "DONE - full report saved in $REPORT (paste it back)"
}

cmd_logs(){
  # Read-only scan of the last N hours of every running container. Counts by level, the most repeated error lines (numbers blanked so the same fault groups together),
  # Dhan error codes, restarts, unhealthy containers, and the known trouble phrases. Needs no login. Output also goes to $LOGREPORT; paste that file back.
  local H="${1:-12}" LOGREPORT="${LOGREPORT:-$HOME/vm_logs.txt}"
  case "$H" in ''|*[!0-9]*) echo "usage: bash scripts/vm_all.sh logs [hours]"; return 2;; esac
  exec > >(tee "$LOGREPORT") 2>&1
  local SVCS; SVCS=$(docker compose ps --services --status running 2>/dev/null)
  [ -n "$SVCS" ] || { echo "no running compose services (is docker compose up?)"; return 1; }
  hr "logs 1. when, code version, container state (restarts, health)"
  date; git log --oneline -1
  docker compose ps --format 'table {{.Service}}\t{{.Status}}' 2>/dev/null | head -30
  for s in $SVCS; do
    c=$(docker compose ps -q "$s" 2>/dev/null | head -1); [ -n "$c" ] || continue
    printf '  %-34s restarts=%s oom=%s\n' "$s" "$(docker inspect -f '{{.RestartCount}}' "$c" 2>/dev/null)" "$(docker inspect -f '{{.State.OOMKilled}}' "$c" 2>/dev/null)"
  done
  hr "logs 2. per service, last ${H} h: lines at each level (expect small numbers; ERROR / CRITICAL / Traceback are the ones to read)"
  for s in $SVCS; do
    L=$(docker compose logs --since "${H}h" --no-log-prefix "$s" 2>&1)
    printf '  %-34s lines=%-7s ERROR=%-5s CRITICAL=%-4s Traceback=%-4s WARNING=%-6s\n' "$s" "$(printf '%s\n' "$L" | wc -l)" \
      "$(printf '%s\n' "$L" | grep -c -E ' ERROR |\[ERROR\]|ERROR:')" "$(printf '%s\n' "$L" | grep -c -E 'CRITICAL')" "$(printf '%s\n' "$L" | grep -c 'Traceback')" "$(printf '%s\n' "$L" | grep -c -E ' WARNING |\[WARNING\]|WARNING:')"
  done
  hr "logs 3. the most repeated ERROR / CRITICAL lines per trading service and the gateway (digits blanked; count, then the line)"
  for s in position-stocks-service real-trade-service api-gateway market-data-service calibration-runner-service; do
    echo "--- $s"
    docker compose logs --since "${H}h" --no-log-prefix "$s" 2>&1 | grep -E ' ERROR |\[ERROR\]|ERROR:|CRITICAL' | sed -E 's/^[0-9T:., +-]+//; s/[0-9]+(\.[0-9]+)?/N/g' | cut -c1-200 | sort | uniq -c | sort -rn | head -8
  done
  hr "logs 4. Dhan: error codes and refusals (DH-9xx codes, IP, rate, rejections), per trading service"
  for s in position-stocks-service real-trade-service; do
    echo "--- $s"
    docker compose logs --since "${H}h" --no-log-prefix "$s" 2>&1 | grep -o -E 'DH-9[0-9]{2}|Invalid IP|not whitelisted|rate limit|too many requests|Dhan refused an order call|insufficient fund|circuit limit|not allowed to trade in intraday' | sort | uniq -c | sort -rn | head -10
  done
  hr "logs 5. tracebacks: the last 3 per trading service (the line before and the exception line)"
  for s in position-stocks-service real-trade-service; do
    echo "--- $s"
    docker compose logs --since "${H}h" --no-log-prefix "$s" 2>&1 | grep -B2 -E '^[A-Za-z_.]+(Error|Exception)[: ]' | cut -c1-220 | tail -12
  done
  hr "logs 6. known trouble phrases (each a count; 0 is good)"
  local ALL; ALL=$(mktemp); docker compose logs --since "${H}h" --no-log-prefix > "$ALL" 2>&1
  for pat in "Traceback" "database is locked" "QueuePool limit" "connection pool" "ReadTimeout" "ConnectTimeout" "Too Many Requests" "HTTP 429" "HTTP 503" "kill switch" "DISARM" "disarm" "STOP NOT" "orphan" "phantom" "stuck" "Dhan refused an order call" "ip guard" "DhanModifyCap"; do
    n=$(grep -c -i -- "$pat" "$ALL"); printf '  %-30s %s\n' "$pat" "$n"
  done
  rm -f "$ALL"
  hr "logs 7. host: disk, memory, load"
  df -h / | tail -1; free -m | head -2; uptime
  hr "DONE - log scan saved in $LOGREPORT (paste it back). Read-only: nothing was changed."
}

cmd_halfsell(){
  # E283 / E285 round 98: the half-sell probe. `status` reads only. `run` places REAL orders (buy 2, sell 1, sell 1 of one share-priced-under-500 stock).
  local mode="${1:-}" sym="${2:-}" U P PS
  case "$mode" in status|run) ;; *) echo "usage: bash scripts/vm_all.sh halfsell status | run [SYMBOL]"; return 2;; esac
  if [ "$mode" = "run" ] && [ -z "${HALFSELL_SKIP_WINDOW_CHECK:-}" ]; then
    # E285 round 121: say "outside the window" BEFORE asking for the admin login and the HALFSELL confirmation (the VM report of 1 Nov typed HALFSELL on a Sunday and then read SKIPPED).
    # The service still checks the window itself (and NSE holidays); this only saves a pointless prompt. HALFSELL_NOW="<weekday 1-7> <HHMM>" is for tests.
    local dow hm
    read -r dow hm <<< "${HALFSELL_NOW:-$(TZ=Asia/Kolkata date '+%u %H%M')}"
    if [ "${dow:-1}" -ge 6 ] || [ "$((10#${hm:-0930}))" -lt 930 ] || [ "$((10#${hm:-0930}))" -ge 1430 ]; then
      echo "HALF-SELL PROBE: NOT STARTED - outside the probe window (09:30-14:30 IST, Monday to Friday). Nothing was asked and nothing was sent. Run it again in the window."
      return 0
    fi
  fi
  read -rp "admin user: " U; read -rsp "admin password: " P; echo
  PS=$(curl -s -m 20 -X POST "$BASE/positionstocks/auth/login" -H "Content-Type: application/json" -d "{\"username\":\"$U\",\"password\":\"$P\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null); unset P
  [ -n "$PS" ] || { echo "PS LOGIN FAILED"; return 1; }
  if [ "$mode" = "status" ]; then
    TOKEN="$PS" python3 scripts/dhan_half_sell_probe.py --status --ps-url http://127.0.0.1:8006; return $?
  fi
  hr "half-sell probe: this places REAL orders on Dhan (BUY 2, cancel both legs, SELL 1, SELL stop for 1, SELL 1) on a stock under Rs 500"
  echo "Needs: market hours 09:30-14:30 IST, Position Stocks armed, kill switch not tripped, the symbol flat in Dhan. Keep Dhan's order book open next to this."
  echo "It stops at the first surprise, cleans up (sells only a real long, never while a leg is live) and says CHECK DHAN if it cannot get flat."
  read -rp "Type HALFSELL to place the orders (anything else cancels): " ok
  [ "$ok" = "HALFSELL" ] || { echo "cancelled, nothing was sent"; return 0; }
  local out="$HOME/half_sell_probe_$(date +%Y%m%d_%H%M%S).txt"
  { date; TOKEN="$PS" python3 scripts/dhan_half_sell_probe.py --yes ${sym:+--symbol "$sym"} --ps-url http://127.0.0.1:8006; echo "(exit $?)"
    hr "order_rate part of /live/state (question Q7)"; curl -s -m 30 -H "Authorization: Bearer $PS" "$BASE/positionstocks/live/state" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('order_rate'), indent=1))" 2>/dev/null
    hr "service log lines of the probe (T0 to T6)"; docker compose logs --since 15m --no-log-prefix position-stocks-service 2>&1 | grep -i -E "half-sell|HALFPROBE|protective stop|Cancelling REAL" | tail -60
  } 2>&1 | tee "$out"
  echo; echo "Saved in $out. Paste it back, and LOOK AT DHAN: the position must be flat and no order may be open."
}

cmd_sweep(){
  hr "sweep study (offline, 200-name sample; report -> /tmp/sweep.md)"
  bash scripts/e278_run.sh --sample 200 2>&1 | tail -15; head -60 /tmp/sweep.md 2>/dev/null
}

case "${1:-}" in
  apply)  shift; cmd_apply "$@" ;;
  deploy) cmd_deploy ;;
  test)   cmd_test ;;
  check)  cmd_check ;;
  logs)   shift; cmd_logs "$@" ;;
  halfsell) shift; cmd_halfsell "$@" ;;
  sweep)  cmd_sweep ;;
  all)    shift; cmd_apply "$@" && cmd_deploy && cmd_test && cmd_check ;;
  *) sed -n 2,17p "$0"; exit 2 ;;
esac
