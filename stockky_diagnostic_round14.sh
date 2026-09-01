#!/usr/bin/env bash
###############################################################################
# stockky_full_diag.sh
#
# Comprehensive diagnostic + stress test for Stockky real-trade-service.
# Runs multiple DEMO cycles, tests bulk quote prefetch, captures logs,
# and measures performance to identify stalls (like the 504 bug).
#
# USAGE:
#   chmod +x stockky_full_diag.sh
#   ./stockky_full_diag.sh
#
# ENV overrides:
#   TRADE_URL, GATEWAY_URL, MARKET_URL (defaults to localhost ports)
###############################################################################
set -uo pipefail

TRADE_URL="${TRADE_URL:-http://localhost:8005}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
MARKET_URL="${MARKET_URL:-http://localhost:8001}"

TS="$(date +%Y%m%d_%H%M%S)"
LOGFILE="stockky_full_diag_${TS}.log"

log() { echo -e "$1" | tee -a "$LOGFILE"; }

: > "$LOGFILE"
log "###############################################################"
log "# Stockky Full Diagnostic + Stress Test -- $(date)"
log "# host: $(hostname)  user: $(whoami)"
log "# TRADE_URL=$TRADE_URL"
log "# GATEWAY_URL=$GATEWAY_URL"
log "# MARKET_URL=$MARKET_URL"
log "###############################################################"

# ---------------------------------------------------------------------------
# Helper: timed curl with timeout and logging
# ---------------------------------------------------------------------------
timed_curl() {
  local name="$1" method="$2" url="$3" data="$4" timeout="${5:-30}"
  log "\n--- $name ($method $url) ---"
  local start=$(date +%s.%N)
  local tmpfile=$(mktemp)
  if [[ -n "$data" ]]; then
    curl -s -m "$timeout" -X "$method" -H "Content-Type: application/json" -d "$data" "$url" -o "$tmpfile" -w "%{http_code}" > /tmp/curl_code
  else
    curl -s -m "$timeout" -X "$method" "$url" -o "$tmpfile" -w "%{http_code}" > /tmp/curl_code
  fi
  local end=$(date +%s.%N)
  local code=$(cat /tmp/curl_code)
  local elapsed=$(awk "BEGIN{printf \"%.2f\", $end - $start}")
  log "HTTP $code, ${elapsed}s"
  head -c 500 "$tmpfile" | tee -a "$LOGFILE"
  rm -f "$tmpfile" /tmp/curl_code
}

# ---------------------------------------------------------------------------
# STEP 1: Health checks (parallel)
# ---------------------------------------------------------------------------
log "\n===== 1. HEALTH CHECKS ====="
for port in 8000 8001 8002 8004 8005 8008; do
  curl -s -o /dev/null -w "port $port: %{http_code}\n" -m 5 "http://localhost:$port/health" &
done
wait

# ---------------------------------------------------------------------------
# STEP 2: Component tests (read-only, non-cycle)
# ---------------------------------------------------------------------------
log "\n===== 2. COMPONENT TESTS ====="
timed_curl "Gateway /scan/universe" GET "$GATEWAY_URL/scan/universe" "" 20
timed_curl "Market /quotes/bulk (first 10 symbols)" POST "$MARKET_URL/quotes/bulk" '{"symbols":["RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","SBIN","BHARTIARTL","ITC","KOTAKBANK"]}' 20
timed_curl "Trade /status/DEMO" GET "$TRADE_URL/status/DEMO" "" 10
timed_curl "Trade /adaptive/status" GET "$TRADE_URL/adaptive/status" "" 10
timed_curl "Trade /risk-config/DEMO" GET "$TRADE_URL/risk-config/DEMO" "" 10
timed_curl "Trade /pipeline/status/DEMO" GET "$TRADE_URL/pipeline/status/DEMO" "" 10

# ---------------------------------------------------------------------------
# STEP 3: Circuit breakers before cycle
# ---------------------------------------------------------------------------
log "\n===== 3. CIRCUIT BREAKERS (before cycle) ====="
curl -s -m 10 "$GATEWAY_URL/circuits" | head -c 500 | tee -a "$LOGFILE"

# ---------------------------------------------------------------------------
# STEP 4: RUN DEMO CYCLE (first run) - this is the main test
# ---------------------------------------------------------------------------
log "\n===== 4. DEMO CYCLE (Run 1) ====="
CYCLE_START=$(date +%s.%N)
CYCLE_HTTP=$(curl -s -o /tmp/cycle_response1.json -w "%{http_code}" \
  -X POST "${TRADE_URL}/cycle/run/DEMO" \
  -H "Content-Type: application/json" \
  --max-time 600)
CYCLE_END=$(date +%s.%N)
CYCLE_SECS=$(awk "BEGIN{printf \"%.1f\", $CYCLE_END - $CYCLE_START}")
log "POST /cycle/run/DEMO -> HTTP $CYCLE_HTTP, took ${CYCLE_SECS}s"
if [[ "$CYCLE_SECS" != "" ]] && awk "BEGIN{exit !($CYCLE_SECS > 250)}"; then
  log "  !! WARNING: cycle took >250s (likely bulk-prefetch stall)"
fi
log "--- response body (first 500 chars) ---"
head -c 500 /tmp/cycle_response1.json | tee -a "$LOGFILE"
log "\n--- full response saved at /tmp/cycle_response1.json ---"

# ---------------------------------------------------------------------------
# STEP 5: Post-cycle state after first run
# ---------------------------------------------------------------------------
log "\n===== 5. POST-CYCLE STATE (Run 1) ====="
for endpoint in pipeline/status/DEMO candidates/DEMO positions/DEMO orders/DEMO; do
  log "\n--- /$endpoint ---"
  curl -s -m 10 "http://localhost:8005/$endpoint" | head -c 300 | tee -a "$LOGFILE"
  log ""
done

# ---------------------------------------------------------------------------
# STEP 6: Circuit breakers after first cycle
# ---------------------------------------------------------------------------
log "\n===== 6. CIRCUIT BREAKERS (after Run 1) ====="
curl -s -m 10 "$GATEWAY_URL/circuits" | head -c 500 | tee -a "$LOGFILE"

# ---------------------------------------------------------------------------
# STEP 7: SECOND DEMO CYCLE (to test caching / prefetch improvement)
# ---------------------------------------------------------------------------
log "\n===== 7. DEMO CYCLE (Run 2) ====="
CYCLE_START2=$(date +%s.%N)
CYCLE_HTTP2=$(curl -s -o /tmp/cycle_response2.json -w "%{http_code}" \
  -X POST "${TRADE_URL}/cycle/run/DEMO" \
  -H "Content-Type: application/json" \
  --max-time 600)
CYCLE_END2=$(date +%s.%N)
CYCLE_SECS2=$(awk "BEGIN{printf \"%.1f\", $CYCLE_END2 - $CYCLE_START2}")
log "POST /cycle/run/DEMO (Run 2) -> HTTP $CYCLE_HTTP2, took ${CYCLE_SECS2}s"
if [[ "$CYCLE_SECS2" != "" ]] && awk "BEGIN{exit !($CYCLE_SECS2 > 250)}"; then
  log "  !! WARNING: second cycle also >250s – prefetch or other bottleneck persists"
fi
log "--- response body (first 500 chars) ---"
head -c 500 /tmp/cycle_response2.json | tee -a "$LOGFILE"
log "\n--- full response saved at /tmp/cycle_response2.json ---"

# ---------------------------------------------------------------------------
# STEP 8: Post-cycle state after second run
# ---------------------------------------------------------------------------
log "\n===== 8. POST-CYCLE STATE (Run 2) ====="
for endpoint in pipeline/status/DEMO candidates/DEMO positions/DEMO orders/DEMO; do
  log "\n--- /$endpoint ---"
  curl -s -m 10 "http://localhost:8005/$endpoint" | head -c 300 | tee -a "$LOGFILE"
  log ""
done

# ---------------------------------------------------------------------------
# STEP 9: Capture logs from key services (last 150 lines)
# ---------------------------------------------------------------------------
log "\n===== 9. SERVICE LOGS (last 150 lines) ====="
log "\n--- real-trade-service ---"
docker compose logs --tail=150 real-trade-service 2>&1 | tee -a "$LOGFILE"
log "\n--- api-gateway ---"
docker compose logs --tail=150 api-gateway 2>&1 | tee -a "$LOGFILE"
log "\n--- market-data-service ---"
docker compose logs --tail=150 market-data-service 2>&1 | tee -a "$LOGFILE"

# ---------------------------------------------------------------------------
# STEP 10: Grep for prefetch/timeout patterns
# ---------------------------------------------------------------------------
log "\n===== 10. GREP FOR PREFETCH/TIMEOUT ====="
log "--- real-trade-service: prefetch ---"
docker compose logs real-trade-service 2>&1 | grep -i "prefetch" | tail -30 | tee -a "$LOGFILE"
log "--- real-trade-service: timeout/18s ---"
docker compose logs real-trade-service 2>&1 | grep -i "timeout\|18s" | tail -30 | tee -a "$LOGFILE"
log "--- market-data-service: yfinance ---"
docker compose logs market-data-service 2>&1 | grep -i "yfinance\|breaker\|circuit" | tail -30 | tee -a "$LOGFILE"

# ---------------------------------------------------------------------------
# STEP 11: Additional stress: simulate multiple concurrent /quotes/bulk calls
# (to see if that triggers the same stall pattern)
# ---------------------------------------------------------------------------
log "\n===== 11. STRESS: Concurrent /quotes/bulk (10 symbols each, 5 parallel) ====="
for i in {1..5}; do
  curl -s -m 30 -X POST "$MARKET_URL/quotes/bulk" \
    -H "Content-Type: application/json" \
    -d '{"symbols":["RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","SBIN","BHARTIARTL","ITC","KOTAKBANK"]}' \
    -o /dev/null -w "bulk call $i: %{http_code}, time: %{time_total}s\n" &
done
wait

log "\n###############################################################"
log "# DONE. Log file: $(pwd)/${LOGFILE}"
log "# Please paste the contents of this file back for analysis."
log "###############################################################"