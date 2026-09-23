#!/usr/bin/env bash
# ============================================================
# Stockky Backend Health Check — run on Ubuntu server
# Usage:  bash scripts/healthcheck.sh [position-stocks|real-trade|all]
# ============================================================
set -euo pipefail

SERVICE="${1:-all}"
PS_URL="${POSITION_STOCKS_URL:-http://localhost:8006}"
RT_URL="${REAL_TRADE_URL:-http://localhost:8005}"
NGINX_URL="${NGINX_URL:-http://localhost}"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}  $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
hdr()  { echo -e "\n${YELLOW}=== $1 ===${NC}"; }

# ── 1. Docker containers ────────────────────────────────────────────────────
hdr "Docker containers"
if command -v docker &>/dev/null; then
  docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || warn "docker not accessible"
else
  warn "docker not found"
fi

# ── 2. position-stocks-service health ──────────────────────────────────────
if [[ "$SERVICE" == "position-stocks" || "$SERVICE" == "all" ]]; then
  hdr "position-stocks-service ($PS_URL)"

  # Liveness
  if curl -sf --max-time 5 "$PS_URL/health" >/dev/null 2>&1; then
    ok "GET /health → 200"
  else
    fail "GET /health FAILED (service down or wrong URL)"
  fi

  # Full status
  STATUS=$(curl -sf --max-time 10 "$PS_URL/status" 2>/dev/null || echo "ERROR")
  if [[ "$STATUS" == "ERROR" ]]; then
    fail "GET /status FAILED"
  else
    ok "GET /status → 200"
    echo "  armed:           $(echo "$STATUS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("armed"))')"
    echo "  service_enabled: $(echo "$STATUS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("service_enabled"))')"
    echo "  auto_pilot:      $(echo "$STATUS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("auto_pilot_enabled"))')"
    echo "  market_open:     $(echo "$STATUS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("market_open"))')"
    echo "  last_cycle_run:  $(echo "$STATUS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("last_cycle_run_at"))')"
    echo "  circuit_breaker: $(echo "$STATUS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["circuit_breaker"].get("state") if d.get("circuit_breaker") else "N/A")')"
  fi

  # WS feed
  WS=$(curl -sf --max-time 5 "$PS_URL/ws-status" 2>/dev/null || echo "ERROR")
  if [[ "$WS" == "ERROR" ]]; then
    fail "GET /ws-status FAILED"
  else
    ok "GET /ws-status → 200"
    echo "  connected:         $(echo "$WS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("connected"))')"
    echo "  subscribed_syms:   $(echo "$WS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("subscribed_symbols"))')"
    echo "  last_tick_at:      $(echo "$WS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("last_tick_at"))')"
    echo "  reconnect_attempts:$(echo "$WS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("reconnect_attempts"))')"
  fi

  # Pipeline status
  PIPE=$(curl -sf --max-time 5 "$PS_URL/pipeline/status" 2>/dev/null || echo "ERROR")
  if [[ "$PIPE" == "ERROR" ]]; then
    fail "GET /pipeline/status FAILED"
  else
    ok "GET /pipeline/status → 200"
    echo "  running:     $(echo "$PIPE" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("running"))')"
    echo "  stage:       $(echo "$PIPE" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("stage_label"))')"
  fi

  # Candidates (live scan)
  CANDS=$(curl -sf --max-time 10 "$PS_URL/candidates" 2>/dev/null || echo "ERROR")
  if [[ "$CANDS" == "ERROR" ]]; then
    fail "GET /candidates FAILED"
  else
    COUNT=$(echo "$CANDS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("count",0))')
    MOPEN=$(echo "$CANDS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("market_open"))')
    ok "GET /candidates → 200  (market_open=$MOPEN, count=$COUNT)"
  fi

  # Candidate log (last 5)
  LOG=$(curl -sf --max-time 5 "$PS_URL/candidates/log?limit=5" 2>/dev/null || echo "ERROR")
  if [[ "$LOG" != "ERROR" ]]; then
    ok "GET /candidates/log → 200"
    echo "$LOG" | python3 -c "
import sys, json
rows = json.load(sys.stdin)
for r in rows:
    print(f\"  {r.get('symbol','?'):12s} {r.get('window_source','?'):4s} {r.get('decision','?'):8s} {r.get('reason','')[:60]}\")
"
  fi

  # Positions
  POSITIONS=$(curl -sf --max-time 5 "$PS_URL/positions" 2>/dev/null || echo "ERROR")
  if [[ "$POSITIONS" != "ERROR" ]]; then
    PCOUNT=$(echo "$POSITIONS" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')
    ok "GET /positions → 200  ($PCOUNT position(s))"
  fi
fi

# ── 3. real-trade-service health ────────────────────────────────────────────
if [[ "$SERVICE" == "real-trade" || "$SERVICE" == "all" ]]; then
  hdr "real-trade-service ($RT_URL)"
  if curl -sf --max-time 5 "$RT_URL/health" >/dev/null 2>&1; then
    ok "GET /health → 200"
  else
    fail "GET /health FAILED"
  fi
fi

# ── 4. Nginx / API gateway ──────────────────────────────────────────────────
hdr "Nginx / API gateway ($NGINX_URL)"
if curl -sf --max-time 5 "$NGINX_URL" >/dev/null 2>&1; then
  ok "Nginx responding"
else
  warn "Nginx not responding (may be intentional if not using nginx locally)"
fi

# ── 5. Service logs (last 20 lines each) ────────────────────────────────────
hdr "Recent logs (last 20 lines per service)"
echo "--- position-stocks-service ---"
docker logs --tail 20 stockky-v2-position-stocks-service-1 2>/dev/null \
  || docker logs --tail 20 position-stocks-service 2>/dev/null \
  || docker logs --tail 20 stockky-position-stocks-service-1 2>/dev/null \
  || warn "Could not fetch position-stocks logs (check container name with: docker ps)"
echo "--- real-trade-service ---"
docker logs --tail 20 stockky-v2-real-trade-service-1 2>/dev/null \
  || docker logs --tail 20 real-trade-service 2>/dev/null \
  || docker logs --tail 20 stockky-real-trade-service-1 2>/dev/null \
  || warn "Could not fetch real-trade logs (check container name with: docker ps)"

hdr "Done"
echo "Tip: tail live logs with:"
echo "  docker logs -f stockky-v2-position-stocks-service-1"
echo "  docker logs -f stockky-v2-real-trade-service-1"
echo ""
echo "Check frontend build issues with:"
echo "  docker logs stockky-v2-frontend-1 2>&1 | tail -30"
echo ""
echo "Restart a service without full redeploy:"
echo "  docker compose restart position-stocks-service"
echo "  docker compose restart real-trade-service"
