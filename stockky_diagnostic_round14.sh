#!/usr/bin/env bash
###############################################################################
# stockky_diag_full.sh
#
# Comprehensive diagnostic for real-trade-service + dependencies.
# Tests all major components: candidates, entry, exit, risk, portfolio,
# adaptive, circuit breakers, logs, and cycle timing.
#
# USAGE:
#   chmod +x stockky_diag_full.sh
#   ./stockky_diag_full.sh
#   (then paste the log file content back)
###############################################################################
set -uo pipefail

TRADE_URL="${TRADE_URL:-http://localhost:8005}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
MARKET_URL="${MARKET_URL:-http://localhost:8001}"
ANALYSIS_URL="${ANALYSIS_URL:-http://localhost:8002}"
DECISION_URL="${DECISION_URL:-http://localhost:8004}"
NOTIF_URL="${NOTIF_URL:-http://localhost:8008}"

TS="$(date +%Y%m%d_%H%M%S)"
LOGFILE="stockky_diag_full_${TS}.log"

log() { echo -e "$1" | tee -a "$LOGFILE"; }
log_cmd() { echo -e "\n--- $1 ---" | tee -a "$LOGFILE"; }

: > "$LOGFILE"
log "###############################################################"
log "# Stockky FULL diagnostic -- $(date)"
log "# host: $(hostname)  user: $(whoami)"
log "# TRADE_URL=$TRADE_URL, GATEWAY_URL=$GATEWAY_URL"
log "###############################################################"

# ---------------------------------------------------------------------------
# STEP 0: Health check all services (quick, parallel)
# ---------------------------------------------------------------------------
log_cmd "HEALTH CHECK (all services)"
for port in 8000 8001 8002 8004 8005 8008; do
  curl -s -o /dev/null -w "port $port: %{http_code}\n" -m 5 "http://localhost:$port/health" &
done
wait
log ""

# ---------------------------------------------------------------------------
# STEP 1: Real-trade-service internal state before cycle
# ---------------------------------------------------------------------------
log_cmd "PRE-CYCLE STATE (real-trade-service)"
for endpoint in status/DEMO pipeline/status/DEMO candidates/DEMO positions/DEMO orders/DEMO adaptive/status; do
  log "--- /$endpoint ---"
  curl -s -m 10 "http://localhost:8005/$endpoint" | head -c 300 | tee -a "$LOGFILE"
  log ""
done

# Also check risk config and account
log "--- /risk-config/DEMO ---"
curl -s -m 10 "http://localhost:8005/risk-config/DEMO" | head -c 300 | tee -a "$LOGFILE"
log ""

# ---------------------------------------------------------------------------
# STEP 2: Trigger DEMO cycle with timing
# ---------------------------------------------------------------------------
log_cmd "TRIGGER DEMO CYCLE (600s timeout)"
CYCLE_START=$(date +%s.%N)
CYCLE_HTTP=$(curl -s -o /tmp/cycle_response.json -w "%{http_code}" \
  -X POST "${TRADE_URL}/cycle/run/DEMO" \
  -H "Content-Type: application/json" \
  --max-time 600)
CYCLE_END=$(date +%s.%N)
CYCLE_SECS=$(awk "BEGIN{printf \"%.1f\", $CYCLE_END-$CYCLE_START}")

log "POST /cycle/run/DEMO -> HTTP $CYCLE_HTTP, took ${CYCLE_SECS}s"
if [[ "$CYCLE_SECS" != "" ]] && awk "BEGIN{exit !($CYCLE_SECS > 250)}"; then
  log "  !! WARNING: cycle took >250s – likely bulk-prefetch stall."
fi
log "--- response body (first 2000 chars) ---"
head -c 2000 /tmp/cycle_response.json | tee -a "$LOGFILE"
log "\n--- (full response saved at /tmp/cycle_response.json) ---"

# ---------------------------------------------------------------------------
# STEP 3: Post-cycle state (immediately after cycle returns)
# ---------------------------------------------------------------------------
log_cmd "POST-CYCLE STATE"
for endpoint in pipeline/status/DEMO candidates/DEMO positions/DEMO orders/DEMO adaptive/status; do
  log "--- /$endpoint ---"
  curl -s -m 10 "http://localhost:8005/$endpoint" | head -c 300 | tee -a "$LOGFILE"
  log ""
done

# ---------------------------------------------------------------------------
# STEP 4: Circuit breakers (gateway and real-trade)
# ---------------------------------------------------------------------------
log_cmd "CIRCUIT BREAKER STATES"
log "--- Gateway circuits ---"
curl -s -m 10 "$GATEWAY_URL/circuits" | head -c 500 | tee -a "$LOGFILE"
log ""
log "--- Real-trade circuits (if any) ---"
curl -s -m 10 "$TRADE_URL/circuits" 2>/dev/null | head -c 500 | tee -a "$LOGFILE" || echo "No /circuits endpoint" | tee -a "$LOGFILE"
log ""

# ---------------------------------------------------------------------------
# STEP 5: Dhan connectivity (if credentials set)
# ---------------------------------------------------------------------------
log_cmd "DHAN STATUS (if configured)"
curl -s -m 10 "$TRADE_URL/dhan/status" | head -c 500 | tee -a "$LOGFILE"
log ""

# ---------------------------------------------------------------------------
# STEP 6: Market regime / adaptive thresholds detail
# ---------------------------------------------------------------------------
log_cmd "ADAPTIVE THRESHOLDS DETAIL"
curl -s -m 10 "$TRADE_URL/adaptive/status?detail=1" 2>/dev/null | head -c 500 | tee -a "$LOGFILE" || echo "No detail param" | tee -a "$LOGFILE"
log ""

# ---------------------------------------------------------------------------
# STEP 7: Service logs (last 200 lines of real-trade and api-gateway)
# ---------------------------------------------------------------------------
log_cmd "REAL-TRADE-SERVICE LOGS (last 200 lines)"
docker compose logs --tail=200 real-trade-service 2>&1 | tee -a "$LOGFILE"

log_cmd "API-GATEWAY LOGS (last 200 lines)"
docker compose logs --tail=200 api-gateway 2>&1 | tee -a "$LOGFILE"

# Also check market-data-service logs for any related errors
log_cmd "MARKET-DATA-SERVICE LOGS (last 100 lines)"
docker compose logs --tail=100 market-data-service 2>&1 | tee -a "$LOGFILE"

# ---------------------------------------------------------------------------
# STEP 8: Grep for specific patterns (prefetch, timeout, errors, volume_shock)
# ---------------------------------------------------------------------------
log_cmd "GREP FOR KEY PATTERNS (real-trade logs)"
docker compose logs real-trade-service 2>&1 | grep -iE "prefetch|volume_shock|timeout|18s|error|exception|stall|circuit" | tail -30 | tee -a "$LOGFILE"

log_cmd "GREP FOR VOLUME_SHOCK CANDIDATES"
docker compose logs real-trade-service 2>&1 | grep -i "volume_shock" | tail -20 | tee -a "$LOGFILE"

# ---------------------------------------------------------------------------
# STEP 9: Check if the fix for bulk-prefetch is applied (look for concurrency settings)
# ---------------------------------------------------------------------------
log_cmd "CHECK FOR PREFETCH CONCURRENCY SETTINGS (env)"
docker compose exec real-trade-service env | grep -i "CANDIDATE_BULK" | tee -a "$LOGFILE" || echo "No CANDIDATE_BULK_* env vars set" | tee -a "$LOGFILE"

# ---------------------------------------------------------------------------
# STEP 10: Final summary
# ---------------------------------------------------------------------------
log "\n###############################################################"
log "# DONE. Log file: $(pwd)/${LOGFILE}"
log "# Please paste the contents of this file back."
log "###############################################################"