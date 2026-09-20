#!/usr/bin/env bash
# ==============================================================================
# stockky_full_test.sh — deep functional check of real-trade-service +
# position-stocks-service: every offline unit test (all calculation/logic
# modules), then a DEMO-mode scenario battery (safe, no real money) that
# exercises the risk engine, sizing, and order-flow endpoints, then REAL-mode
# READ-ONLY diagnostics for position-stocks-service (it has no DEMO mode, so
# these are deliberately non-mutating — status/ledger/candidates/reconcile
# reads only, never a live BUY).
#
# Run on the Ubuntu VM, from the repo root (~/stockky-v2).
# Usage:
#   chmod +x stockky_full_test.sh
#   ADMIN_USER=admin ADMIN_PASS='yourpassword' ./stockky_full_test.sh
#
# Everything is logged to ./stockky_test_report_<timestamp>/ — read that
# after it finishes; this script only prints a summary to the terminal.
# ==============================================================================
set -uo pipefail

BASE="https://stockky.duckdns.org"
RT="$BASE/realtrade"
PS="$BASE/positionstocks"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="stockky_test_report_${STAMP}"
mkdir -p "$OUT"/{coverage,demo_scenarios,real_diagnostics}

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-}"

PASS=0
FAIL=0
SKIP=0

note()  { echo -e "\n\033[1;34m== $* ==\033[0m"; }
ok()    { echo -e "\033[1;32m[PASS]\033[0m $*"; PASS=$((PASS+1)); }
bad()   { echo -e "\033[1;31m[FAIL]\033[0m $*"; FAIL=$((FAIL+1)); }
skip()  { echo -e "\033[1;33m[SKIP]\033[0m $*"; SKIP=$((SKIP+1)); }

run() {
  local name="$1" expect="$2" logfile="$3"; shift 3
  local http_code
  http_code=$(curl -s -o "$logfile" -w "%{http_code}" "$@")
  if [ -z "$expect" ]; then
    echo "  -> HTTP $http_code (logged to $logfile)"
    return
  fi
  if [ "$http_code" = "$expect" ]; then
    ok "$name (HTTP $http_code)"
  else
    bad "$name (expected $expect, got $http_code — see $logfile)"
  fi
}

note "PART 1 — offline unit + coverage: real-trade-service"
pushd services/real-trade-service >/dev/null
pip install --break-system-packages -q pytest pytest-cov 2>&1 | tail -3
if python3 -m pytest tests -q --cov=. --cov-report=term-missing \
      --cov-report="html:../../$OUT/coverage/real-trade-service-html" \
      > "../../$OUT/coverage/real-trade-service.log" 2>&1; then
  ok "real-trade-service test suite"
else
  bad "real-trade-service test suite — see $OUT/coverage/real-trade-service.log"
fi
tail -25 "../../$OUT/coverage/real-trade-service.log"
popd >/dev/null

note "PART 1 — offline unit + coverage: position-stocks-service"
pushd services/position-stocks-service >/dev/null
pip install --break-system-packages -q pytest pytest-cov 2>&1 | tail -3
if python3 -m pytest tests -q --cov=. --cov-report=term-missing \
      --cov-report="html:../../$OUT/coverage/position-stocks-service-html" \
      > "../../$OUT/coverage/position-stocks-service.log" 2>&1; then
  ok "position-stocks-service test suite"
else
  bad "position-stocks-service test suite — see $OUT/coverage/position-stocks-service.log"
fi
tail -25 "../../$OUT/coverage/position-stocks-service.log"
popd >/dev/null

echo
echo "Coverage HTML reports: $OUT/coverage/*-html/index.html"
echo "(scp these to your laptop and open in a browser — red lines are"
echo " calculation paths NEVER exercised by any test; that's your real gap list.)"

note "PART 2 — real-trade-service DEMO-mode scenarios"

if [ -z "$ADMIN_PASS" ]; then
  skip "DEMO scenarios needing admin auth — set ADMIN_PASS env var and re-run"
else
  TOKEN=$(curl -s -X POST "$RT/auth/login" -H "Content-Type: application/json" \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
  if [ -z "$TOKEN" ]; then
    bad "login for real-trade-service DEMO scenarios (check ADMIN_USER/ADMIN_PASS)"
  else
    AUTH=(-H "Authorization: Bearer $TOKEN")

    run "status/DEMO"            200 "$OUT/demo_scenarios/status.json"            "$RT/status/DEMO" "${AUTH[@]}"
    run "positions/DEMO"         200 "$OUT/demo_scenarios/positions.json"         "$RT/positions/DEMO" "${AUTH[@]}"
    run "candidates/DEMO"        200 "$OUT/demo_scenarios/candidates.json"        "$RT/candidates/DEMO" "${AUTH[@]}"
    run "pipeline/status/DEMO"   200 "$OUT/demo_scenarios/pipeline.json"          "$RT/pipeline/status/DEMO" "${AUTH[@]}"
    run "adaptive/status"        200 "$OUT/demo_scenarios/adaptive.json"          "$RT/adaptive/status" "${AUTH[@]}"
    run "risk-config/DEMO"       200 "$OUT/demo_scenarios/risk_config.json"       "$RT/risk-config/DEMO" "${AUTH[@]}"
    run "orders/DEMO"            200 "$OUT/demo_scenarios/orders.json"            "$RT/orders/DEMO" "${AUTH[@]}"
    run "reconcile/DEMO (POST)"  200 "$OUT/demo_scenarios/reconcile.json" -X POST "$RT/reconcile/DEMO" "${AUTH[@]}"

    scenario() {
      local label="$1" json="$2"
      curl -s -X POST -H "Content-Type: application/json" "${AUTH[@]}" \
        "$RT/risk-engine/check" -d "$json" \
        | python3 -m json.tool > "$OUT/demo_scenarios/risk_${label}.json" 2>/dev/null
      echo "  scenario '$label' -> $OUT/demo_scenarios/risk_${label}.json"
    }
    note "risk-engine/check scenario matrix (DEMO)"
    scenario "normal_buy"        '{"mode":"DEMO","symbol":"RELIANCE","side":"BUY","qty":1,"entry_price":2900,"stop_price":2850}'
    scenario "penny_stock"       '{"mode":"DEMO","symbol":"SUZLON","side":"BUY","qty":1,"entry_price":8,"stop_price":7.8}'
    scenario "huge_qty"          '{"mode":"DEMO","symbol":"RELIANCE","side":"BUY","qty":100000,"entry_price":2900,"stop_price":2850}'
    scenario "stop_above_entry_buy"  '{"mode":"DEMO","symbol":"RELIANCE","side":"BUY","qty":1,"entry_price":2900,"stop_price":2950}'
    scenario "stop_below_entry_sell" '{"mode":"DEMO","symbol":"RELIANCE","side":"SELL","qty":1,"entry_price":2900,"stop_price":2850}'
    scenario "zero_price"        '{"mode":"DEMO","symbol":"RELIANCE","side":"BUY","qty":1,"entry_price":0,"stop_price":0}'
    scenario "tight_stop"        '{"mode":"DEMO","symbol":"RELIANCE","side":"BUY","qty":1,"entry_price":2900,"stop_price":2898}'
    echo "  -> Read each risk_*.json and check the 'allowed'/'rejection_reason' field"
    echo "     matches what SHOULD happen for that scenario. This is the real test."

    curl -s -X POST -H "Content-Type: application/json" "${AUTH[@]}" \
      "$RT/manual-order/DEMO/preview" \
      -d '{"symbol":"RELIANCE","side":"BUY","qty":1,"entry_price":2900,"stop_price":2850}' \
      | python3 -m json.tool > "$OUT/demo_scenarios/manual_order_preview.json" 2>/dev/null
    echo "  manual-order preview -> $OUT/demo_scenarios/manual_order_preview.json"
  fi
fi

note "PART 3 — position-stocks-service REAL read-only diagnostics"

if [ -z "$ADMIN_PASS" ]; then
  skip "position-stocks-service diagnostics needing admin auth — set ADMIN_PASS and re-run"
else
  PTOKEN=$(curl -s -X POST "$PS/auth/login" -H "Content-Type: application/json" \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
  if [ -z "$PTOKEN" ]; then
    bad "login for position-stocks-service diagnostics"
  else
    PAUTH=(-H "Authorization: Bearer $PTOKEN")
    run "status"             200 "$OUT/real_diagnostics/status.json"          "$PS/status" "${PAUTH[@]}"
    run "ledger"             200 "$OUT/real_diagnostics/ledger.json"          "$PS/ledger" "${PAUTH[@]}"
    run "positions"          200 "$OUT/real_diagnostics/positions.json"       "$PS/positions" "${PAUTH[@]}"
    run "candidates"         200 "$OUT/real_diagnostics/candidates.json"      "$PS/candidates" "${PAUTH[@]}"
    run "candidates/log"     200 "$OUT/real_diagnostics/candidates_log.json"  "$PS/candidates/log" "${PAUTH[@]}"
    run "candidates/restricted" 200 "$OUT/real_diagnostics/restricted.json"   "$PS/candidates/restricted" "${PAUTH[@]}"
    run "trades/history"     200 "$OUT/real_diagnostics/trades_history.json" "$PS/trades/history" "${PAUTH[@]}"
    run "reconcile/pending"  200 "$OUT/real_diagnostics/reconcile_pending.json" "$PS/reconcile/pending" "${PAUTH[@]}"
    run "pipeline/status"    200 "$OUT/real_diagnostics/pipeline.json"        "$PS/pipeline/status" "${PAUTH[@]}"
    run "dhan/live-orders"   200 "$OUT/real_diagnostics/live_orders.json"     "$PS/dhan/live-orders" "${PAUTH[@]}"
    run "ws-status"          200 "$OUT/real_diagnostics/ws_status.json"       "$PS/ws-status" "${PAUTH[@]}"
    run "auth/config-check"  200 "$OUT/real_diagnostics/auth_config.json"     "$PS/auth/config-check" "${PAUTH[@]}"
  fi
fi

note "SUMMARY"
echo "PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP"
echo "Full logs + coverage HTML in: $OUT/"
echo
echo "What to actually look at, in order of value:"
echo "  1. $OUT/coverage/*.log — any pytest FAILED line = a real bug"
echo "  2. $OUT/coverage/*-html/index.html — red lines = calculation code no test ever runs"
echo "  3. $OUT/demo_scenarios/risk_*.json — does each 'allowed'/'rejection_reason' make sense?"
echo "  4. $OUT/real_diagnostics/*.json — anything unexpected in a live number (ledger drift,"
echo "     a candidate stuck in candidates/restricted that shouldn't be, reconcile/pending non-empty)"
