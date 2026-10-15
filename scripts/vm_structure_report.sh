#!/usr/bin/env bash
# ============================================================
# Stockky VM structure / broker-stop report (group386). Run on the VM from ~/stockky-v2:
#
#   bash scripts/vm_structure_report.sh            # writes vm_report.txt and prints it (each section capped at CAP bytes)
#   CAP=4000 bash scripts/vm_structure_report.sh   # longer sections
#   ADMIN_TOKEN=<real-trade session token> bash scripts/vm_structure_report.sh   # adds the REAL-mode real-trade sections
#
# Read-only: GET requests and `docker compose` reads only, and the .env lines it echoes are the structure / broker-stop
# settings (no secrets). Every call prints its HTTP status and time, so an empty section can be told apart from a 404, a
# timeout or a refused connection. Routes were checked against docker-compose.yml and the services' route files:
#   decision-prediction 8004 (mounts /decision, /training, /prediction) - api-gateway 8000 - market-data 8001
#   analysis-intelligence 8002 (mounts /news ...) - real-trade 8005 - position-stocks 8006
# Test hooks: CURL_BIN, DOCKER_BIN, ENV_FILE, OUT.
# ============================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
CURL_BIN="${CURL_BIN:-curl}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
OUT="${OUT:-$REPO_DIR/vm_report.txt}"
CAP="${CAP:-1500}"
SINCE="${SINCE:-24h}"

: > "$OUT"

section() { printf '=== %s\n' "$1" >> "$OUT"; }
cap_to_out() { head -c "$CAP" >> "$OUT"; printf '\n\n' >> "$OUT"; }

# call <label> <url> [auth-token]  - prints "HTTP <code> in <seconds>s" then the body, capped.
call() {
  local label="$1" url="$2" token="${3:-}" body code t
  local -a args=(-s -m 25 -w '\n%{http_code} %{time_total}' )
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  section "$label  ($url)"
  body="$("$CURL_BIN" "${args[@]}" "$url" 2>&1)"; local rc=$?
  if [[ $rc -ne 0 && -z "$body" ]]; then
    printf 'curl failed (exit %s): no connection, refused or timed out after 25 s\n' "$rc" | cap_to_out; return
  fi
  code="$(printf '%s' "$body" | tail -n1)"
  body="$(printf '%s' "$body" | sed '$d')"
  { printf 'HTTP %s in %ss\n' "${code%% *}" "${code##* }"; printf '%s\n' "$body"; } | cap_to_out
}

# run <label> <command...>
run() { local label="$1"; shift; section "$label"; "$@" 2>&1 | cap_to_out; }

section "date"; date -u '+%Y-%m-%d %H:%M:%S UTC' >> "$OUT"; printf '\n' >> "$OUT"

# compose ps is shown as bare service names + state so the 7 services fit in one capped section
section "services"
"$DOCKER_BIN" compose ps --format '{{.Service}}  {{.Status}}' 2>&1 | cap_to_out

section "settings (.env lines; a missing line means the code default applies)"
{ grep -E '^(BROKER_STOP|STRUCTURE_|SNIPER_STRUCTURE|SNIPER_PREPARE|BOARD_USE_MS)' "$ENV_FILE" 2>/dev/null || echo "(none of these are set in $ENV_FILE - defaults apply)"; } | cap_to_out

# structure evidence
call "structure report (decision/training)"   "localhost:8004/training/api/structure/report"
call "observation stats (decision)"           "localhost:8004/decision/structure/observation-stats"
call "structure refresh status (gateway)"     "localhost:8000/data-feed/structure-refresh/status"
call "structure of one stock (gateway)"       "localhost:8000/stock/INFY/structure"
call "real-trade structure DEMO"              "localhost:8005/structure/status/DEMO"
call "real-trade observations DEMO"           "localhost:8005/structure/observations/DEMO"
if [[ -n "${ADMIN_TOKEN:-}" ]]; then
  call "real-trade structure REAL"            "localhost:8005/structure/status/REAL" "$ADMIN_TOKEN"
  call "real-trade observations REAL"         "localhost:8005/structure/observations/REAL" "$ADMIN_TOKEN"
else
  section "real-trade structure REAL / observations REAL"; printf 'skipped: set ADMIN_TOKEN (real-trade /auth/login session token) to include them\n\n' >> "$OUT"
fi
call "position-stocks structure"              "localhost:8006/structure/status"
call "position-stocks structure edge"         "localhost:8006/structure/edge"

# earlier open questions
call "db latency gateway"                     "localhost:8000/ops/db-latency"
call "db latency real-trade"                  "localhost:8005/ops/db-latency"
call "db latency market-data"                 "localhost:8001/ops/db-latency"
call "news feed health"                       "localhost:8002/news/feed-health"
call "decision for INFY (decision service)"   "localhost:8004/decision/decide/INFY"

run "broker stop log (${SINCE})" sh -c "'$DOCKER_BIN' compose logs --since '$SINCE' real-trade-service 2>&1 | grep -i -E 'broker-stop|BROKER_STOP|SAFETY' | tail -30"
run "structure log (${SINCE})"   sh -c "'$DOCKER_BIN' compose logs --since '$SINCE' api-gateway 2>&1 | grep -i 'structure' | tail -15"

echo "done: $(wc -c < "$OUT") bytes in $OUT"
cat "$OUT"
