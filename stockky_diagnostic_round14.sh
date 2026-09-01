#!/usr/bin/env bash
###############################################################################
# stockky_diagnostic_round14.sh
#
# ONE script, ONE log file. Run this, then paste/upload the log file back
# to Claude (in this chat) and I'll go through it and fix everything found
# in one pass.
#
# WHAT THIS DOES (in order):
#   0. Runs the existing stockky_test_all.sh read-only sweep (health +
#      every safe endpoint on every service, syntax-checks every .py file)
#   1. Triggers a REAL, FULL pipeline cycle in DEMO mode
#        -> POST /cycle/run/DEMO
#        This pulls REAL live market data and runs the actual candidate
#        picking (candidate_engine), entry timing (entry_engine), and
#        exit/sell logic (exit_engine) end-to-end, exactly like a real
#        cycle does -- it just trades with the simulated DEMO wallet, not
#        real money. This is what will actually exercise "how we pick the
#        stock and when we sell" with real data.
#   2. Times that cycle precisely (this is how we confirm/deny the 504
#      Pipeline-tab timeout from your last screenshot -- REAL mode hit
#      323.2s against nginx's 300s limit; we check where DEMO lands).
#   3. Pulls the results of that cycle: candidates picked, any positions
#      opened/closed, pipeline status, circuit breaker states, adaptive
#      threshold status.
#   4. Writes ALL of the above -- every request, status code, timing, and
#      response body -- into one timestamped log file.
#
# WHAT THIS DELIBERATELY DOES NOT DO
#   - Never touches REAL mode. No real orders, no real broker calls, no
#     real money at risk, anywhere in this script.
#   - Never arms/disarms anything or changes risk-config.
#
# USAGE
#   chmod +x stockky_diagnostic_round14.sh
#   ./stockky_diagnostic_round14.sh
#   (then send me the printed .log file path's contents)
#
# ENV OVERRIDES (same as stockky_test_all.sh; sane docker-compose defaults
# apply if unset)
#   GATEWAY_URL MARKET_URL ANALYSIS_URL DECISION_URL NOTIF_URL TRADE_URL
###############################################################################
set -uo pipefail

TRADE_URL="${TRADE_URL:-http://localhost:8005}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"

TS="$(date +%Y%m%d_%H%M%S)"
LOGFILE="stockky_diagnostic_${TS}.log"

log() { echo -e "$1" | tee -a "$LOGFILE"; }

: > "$LOGFILE"
log "###############################################################"
log "# Stockky full diagnostic -- $(date)"
log "# host: $(hostname)  user: $(whoami)"
log "# TRADE_URL=$TRADE_URL  GATEWAY_URL=$GATEWAY_URL"
log "###############################################################"

# ---------------------------------------------------------------------------
# STEP 0: existing safe read-only sweep, if present
# ---------------------------------------------------------------------------
if [[ -f "./stockky_test_all.sh" ]]; then
  log "\n===== STEP 0: stockky_test_all.sh (read-only sweep) ====="
  chmod +x ./stockky_test_all.sh
  # tee (not >>) so you see live progress instead of a silent hang;
  # timeout guards against wait_for looping forever if a service (e.g.
  # frontend on :5173) simply isn't deployed here
  timeout 300 ./stockky_test_all.sh 2>&1 | tee -a "$LOGFILE"
  RC=${PIPESTATUS[0]}
  if [[ $RC -eq 124 ]]; then
    log "!! stockky_test_all.sh hit the 300s guard and was killed — likely stuck waiting on a service that isn't up (check which 'waiting for ...' line printed last above)."
  fi
  log "----- end of stockky_test_all.sh output -----"
else
  log "\n===== STEP 0: SKIPPED (stockky_test_all.sh not found in $(pwd)) ====="
fi

# ---------------------------------------------------------------------------
# STEP 1: trigger a real DEMO cycle with real market data, timed precisely
# ---------------------------------------------------------------------------
log "\n===== STEP 1: full pipeline cycle in DEMO mode (real market data, simulated wallet) ====="
CYCLE_START=$(date +%s.%N)
CYCLE_HTTP_CODE=$(curl -s -o /tmp/stockky_cycle_demo.json -w "%{http_code}" \
  -X POST "${TRADE_URL}/cycle/run/DEMO" \
  -H "Content-Type: application/json" \
  --max-time 600)
CYCLE_END=$(date +%s.%N)
CYCLE_SECS=$(awk "BEGIN{printf \"%.1f\", $CYCLE_END-$CYCLE_START}")

log "POST /cycle/run/DEMO -> HTTP $CYCLE_HTTP_CODE, took ${CYCLE_SECS}s"
if [[ "$CYCLE_SECS" != "" ]] && awk "BEGIN{exit !($CYCLE_SECS > 250)}"; then
  log "  !! WARNING: this is approaching/over typical nginx proxy_read_timeout (300s). Note this in what you send back."
fi
log "--- response body (first 4000 chars) ---"
head -c 4000 /tmp/stockky_cycle_demo.json | tee -a "$LOGFILE"
log "\n--- (full response saved separately at /tmp/stockky_cycle_demo.json) ---"

# ---------------------------------------------------------------------------
# STEP 2: pull the results of that cycle
# ---------------------------------------------------------------------------
log "\n===== STEP 2: post-cycle state ====="

for pair in \
  "GET|${TRADE_URL}/pipeline/status/DEMO|pipeline status" \
  "GET|${TRADE_URL}/candidates/DEMO|candidates picked" \
  "GET|${TRADE_URL}/positions/DEMO|open/closed positions" \
  "GET|${TRADE_URL}/orders/DEMO|orders" \
  "GET|${TRADE_URL}/adaptive/status|adaptive thresholds" \
  "GET|${GATEWAY_URL}/circuits|circuit breaker states" \
  ; do
  METHOD="${pair%%|*}"; rest="${pair#*|}"; URL="${rest%%|*}"; DESC="${rest#*|}"
  log "\n--- $DESC ($METHOD $URL) ---"
  T0=$(date +%s.%N)
  BODY=$(curl -s -m 30 -w "\n__HTTP_CODE__:%{http_code}" -X "$METHOD" "$URL")
  T1=$(date +%s.%N)
  SECS=$(awk "BEGIN{printf \"%.1f\", $T1-$T0}")
  CODE=$(echo "$BODY" | grep -o '__HTTP_CODE__:[0-9]*' | cut -d: -f2)
  echo "$BODY" | sed 's/__HTTP_CODE__:[0-9]*$//' | head -c 3000 | tee -a "$LOGFILE"
  log "\n(HTTP $CODE, ${SECS}s)"
done

log "\n###############################################################"
log "# DONE. Log file: $(pwd)/${LOGFILE}"
log "# Send that file (or paste its contents) back in chat."
log "###############################################################"
