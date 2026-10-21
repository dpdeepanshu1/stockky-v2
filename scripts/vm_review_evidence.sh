#!/usr/bin/env bash
# ============================================================
# Stockky VM review-evidence report (group 482). One command that gathers what the 21 Oct review is waiting on. Run on the VM from ~/stockky-v2:
#
#   ADMIN_TOKEN=<real-trade session token> bash scripts/vm_review_evidence.sh      # writes review_evidence.txt and prints it
#   CAP=6000 ADMIN_TOKEN=... bash scripts/vm_review_evidence.sh                    # longer sections
#   DAYS=30 SINCE=2026-10-20 ADMIN_TOKEN=... bash scripts/vm_review_evidence.sh   # trade window / charges-split start day
#
# Collects, in this order: the review settings from .env (no secrets), both /review-fixes/status views (size ladder level, stop-out pause, ORB
# counters), the real-trade charges ledger by component, scripts/charges_split.py and scripts/records_report.py on a fresh export of REAL trades,
# and scripts/gate_scorecard.py inside the position-stocks container (did the gates block winners?). Paste review_evidence.txt back.
# Read-only: GET requests, `docker compose exec` of a read-only script, and local python on the downloaded export. Every call prints its HTTP status and
# time, so an empty section can be told apart from a 401, a 404 or a timeout. Ports: real-trade 8005, position-stocks 8006 (docker-compose.yml).
# Without ADMIN_TOKEN the REAL sections are skipped and the report says so. Test hooks: CURL_BIN, DOCKER_BIN, PYTHON_BIN, ENV_FILE, OUT, REC.
# ============================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
CURL_BIN="${CURL_BIN:-curl}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
OUT="${OUT:-$REPO_DIR/review_evidence.txt}"
REC="${REC:-$REPO_DIR/rt_review_evidence.json}"
CAP="${CAP:-3500}"
DAYS="${DAYS:-60}"
SINCE="${SINCE:-}"

: > "$OUT"
section() { printf '=== %s\n' "$1" >> "$OUT"; }
cap_to_out() { head -c "$CAP" >> "$OUT"; printf '\n\n' >> "$OUT"; }

# call <label> <url> [token]  - prints "HTTP <code> in <seconds>s" then the body, capped.
call() {
  local label="$1" url="$2" token="${3:-}" body code
  local -a args=(-s -m 40 -w '\n%{http_code} %{time_total}')
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  section "$label  ($url)"
  body="$("$CURL_BIN" "${args[@]}" "$url" 2>&1)"; local rc=$?
  if [[ $rc -ne 0 && -z "$body" ]]; then
    printf 'curl failed (exit %s): no connection, refused or timed out after 40 s\n' "$rc" | cap_to_out; return
  fi
  code="$(printf '%s' "$body" | tail -n1)"
  body="$(printf '%s' "$body" | sed '$d')"
  { printf 'HTTP %s in %ss\n' "${code%% *}" "${code##* }"; printf '%s\n' "$body"; } | cap_to_out
}

run() { local label="$1"; shift; section "$label"; "$@" 2>&1 | cap_to_out; }

section "date"; date -u '+%Y-%m-%d %H:%M:%S UTC' >> "$OUT"; printf '\n' >> "$OUT"

section "services"
"$DOCKER_BIN" compose ps --format '{{.Service}}  {{.Status}}' 2>&1 | cap_to_out

section "settings (.env lines; a missing line means the code default applies)"
{ grep -E '^(STOPOUT_|ENTRY_MIN_QTY|ENTRY_LAST_TIME_IST|SCALP_ENTRY_CUTOFF_IST|ORB_MODE|OVERNIGHT_HOLD_ENABLED|BROKER_STOP|SIZE_LADDER_|SCALP_COST_GATE_|ENTRY_PRODUCT_MODE|MAX_TRADE_VALUE|OPENING_)' "$ENV_FILE" 2>/dev/null || echo "(none of these are set in $ENV_FILE - code defaults apply)"; } | cap_to_out

call "position-stocks review status"  "localhost:8006/review-fixes/status" "${ADMIN_TOKEN:-}"

if [[ -n "${ADMIN_TOKEN:-}" ]]; then
  call "real-trade review status"     "localhost:8005/review-fixes/status" "$ADMIN_TOKEN"
  call "real-trade charges by component" "localhost:8005/charges/REAL/cumulative?days=30" "$ADMIN_TOKEN"
  section "REAL trade export (last $DAYS days) -> $REC"
  rbody="$("$CURL_BIN" -s -m 90 -w '\n%{http_code}' -H "Authorization: Bearer $ADMIN_TOKEN" "localhost:8005/positions/REAL/records?days=$DAYS&limit=1000" 2>&1)"; rrc=$?
  rcode="$(printf '%s' "$rbody" | tail -n1)"
  if [[ $rrc -ne 0 || "$rcode" != "200" ]]; then
    printf 'export failed (curl exit %s, HTTP %s): the two trade reports below are skipped\n' "$rrc" "${rcode:-none}" | cap_to_out
  else
    printf '%s' "$rbody" | sed '$d' > "$REC"
    printf 'saved %s bytes to %s\n' "$(wc -c < "$REC" | tr -d ' ')" "$REC" | cap_to_out
    sinceargs=(); [[ -n "$SINCE" ]] && sinceargs=(--since "$SINCE")
    run "charges split: same-day vs carried, by size (scripts/charges_split.py)" "$PYTHON_BIN" "$REPO_DIR/scripts/charges_split.py" "$REC" ${sinceargs[@]+"${sinceargs[@]}"}
    run "trade report (scripts/records_report.py)" "$PYTHON_BIN" "$REPO_DIR/scripts/records_report.py" "$REC" ${sinceargs[@]+"${sinceargs[@]}"}
  fi
else
  section "REAL sections (real-trade status, charges, trade export, trade reports)"
  printf 'skipped: set ADMIN_TOKEN (real-trade /auth/login session token) to include them\n\n' >> "$OUT"
fi

section "gate scorecard: did the position-stocks gates block winners? (scripts/gate_scorecard.py, last 15 days)"
"$DOCKER_BIN" compose exec -T position-stocks-service python - --days 15 < "$REPO_DIR/scripts/gate_scorecard.py" 2>&1 | cap_to_out

cat "$OUT"
