#!/usr/bin/env bash
###############################################################################
# stockky_test_all.sh
#
# One-shot functional test sweep for the whole Stockky v2 5-service stack
# (market-data, analysis-intelligence, decision-prediction, api-gateway,
# notification-scheduler, real-trade-service) + frontend.
#
# WHAT IT DOES
#   0. Compiles every .py file in the repo (catches syntax errors instantly)
#   1. Waits for / confirms every service is up
#   2. Hits every SAFE, read-only endpoint on every service with real test
#      symbols and logs status code + response time + response snippet
#   3. Optionally (--with-writes) does a couple of safe, reversible writes
#      (watchlist add/remove roundtrip, notification test ping)
#   4. Prints a PASS/WARN/FAIL summary and a full log file you can hand back
#
# WHAT IT DELIBERATELY DOES NOT DO (by default)
#   - Never calls anything under real-trade-service that can arm trading,
#     place/cancel orders, or run a live cycle (money risk).
#   - Never triggers the full universe /scan, /stockky-hot/run, training
#     retrain, or data-feed hard-reset/purge (slow, heavy, or destructive).
#   - Never fires real Telegram/Discord/CallMeBot notifications unless you
#     pass --with-writes (and even then only /notifications/test).
#   These are listed at the end under "NOT AUTO-TESTED" so you know what
#   still needs a manual click-through.
#
# USAGE
#   chmod +x stockky_test_all.sh
#   ./stockky_test_all.sh                     # safe read-only sweep (default)
#   ./stockky_test_all.sh --with-writes        # + watchlist roundtrip + notif test
#   ./stockky_test_all.sh --project-dir /path/to/stockky-v2-main   # for syntax check
#   ./stockky_test_all.sh --base-url https://stockky.duckdns.org   # test via nginx/domain instead of localhost ports
#
# ENV OVERRIDES (used if set, otherwise sane docker-compose defaults apply)
#   GATEWAY_URL MARKET_URL ANALYSIS_URL DECISION_URL NOTIF_URL TRADE_URL FRONTEND_URL
###############################################################################
set -uo pipefail

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
PROJECT_DIR="${PROJECT_DIR:-}"
WITH_WRITES=0
BASE_URL_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-writes) WITH_WRITES=1; shift ;;
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --base-url) BASE_URL_OVERRIDE="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^#//'; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# AUTO-DETECT PROJECT_DIR if not given
#   1. current directory, if it has docker-compose.yml + services/
#   2. ./stockky-v2-main under current dir
#   3. search $HOME up to depth 4
# ---------------------------------------------------------------------------
if [[ -z "$PROJECT_DIR" ]]; then
  if [[ -f "./docker-compose.yml" && -d "./services" ]]; then
    PROJECT_DIR="$(pwd)"
  elif [[ -f "./stockky-v2-main/docker-compose.yml" ]]; then
    PROJECT_DIR="$(pwd)/stockky-v2-main"
  else
    FOUND="$(find "$HOME" -maxdepth 4 -iname "stockky-v2-main" -type d 2>/dev/null | head -1)"
    PROJECT_DIR="${FOUND:-$HOME/stockky-v2-main}"
  fi
fi

# ---------------------------------------------------------------------------
# AUTO-DETECT HOST IP (informational — shown in the header so you can
# eyeball it against what you expect on the Oracle VM; does not change
# any URL by itself unless you pass --base-url)
# ---------------------------------------------------------------------------
HOST_IP_PRIVATE="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP_PRIVATE="${HOST_IP_PRIVATE:-unknown}"

if [[ -n "$BASE_URL_OVERRIDE" ]]; then
  # Single domain fronted by nginx (deploy/nginx-stockky.conf style), e.g. https://stockky.duckdns.org
  # Assumes /api -> gateway. Adjust the *_URL exports below if your nginx map differs.
  GATEWAY_URL="${GATEWAY_URL:-$BASE_URL_OVERRIDE/api}"
  MARKET_URL="${MARKET_URL:-$BASE_URL_OVERRIDE/api}"
  ANALYSIS_URL="${ANALYSIS_URL:-$BASE_URL_OVERRIDE/api}"
  DECISION_URL="${DECISION_URL:-$BASE_URL_OVERRIDE/api}"
  NOTIF_URL="${NOTIF_URL:-$BASE_URL_OVERRIDE/api}"
  TRADE_URL="${TRADE_URL:-$BASE_URL_OVERRIDE/api}"
  FRONTEND_URL="${FRONTEND_URL:-$BASE_URL_OVERRIDE}"
else
  GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
  MARKET_URL="${MARKET_URL:-http://localhost:8001}"
  ANALYSIS_URL="${ANALYSIS_URL:-http://localhost:8002}"
  DECISION_URL="${DECISION_URL:-http://localhost:8004}"
  NOTIF_URL="${NOTIF_URL:-http://localhost:8008}"
  TRADE_URL="${TRADE_URL:-http://localhost:8005}"
  FRONTEND_URL="${FRONTEND_URL:-http://localhost:5173}"
fi

SYM="RELIANCE"
SYM2="TCS"
MODE="DEMO"
TIMEOUT=25
STARTUP_WAIT=90   # seconds to wait for services to come up before giving up

TS="$(date +%Y%m%d_%H%M%S)"
LOGDIR="$HOME/stockky_test_results"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/test_${TS}.log"
FAILFILE="$LOGDIR/failures_${TS}.log"
: > "$LOGFILE"
: > "$FAILFILE"

PASS=0; WARN=0; FAIL=0; SKIP=0
START_TS=$(date +%s)

# ---------------------------------------------------------------------------
# COLORS / LOGGING
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_G="\033[32m"; C_Y="\033[33m"; C_R="\033[31m"; C_B="\033[34m"; C_N="\033[0m"; C_BOLD="\033[1m"
else
  C_G=""; C_Y=""; C_R=""; C_B=""; C_N=""; C_BOLD=""
fi

log()  { echo -e "$1" | tee -a "$LOGFILE"; }
sect() { log "\n${C_BOLD}${C_B}=== $1 ===${C_N}"; }

# ---------------------------------------------------------------------------
# CORE TEST FUNCTION
# check NAME METHOD URL [EXPECTED_2XX_ONLY:0|1] [JSON_BODY]
#
# Classification:
#   2xx                          -> PASS
#   400/401/403/404/405/422/429  -> WARN  (reachable; likely expected without auth/data/params)
#   5xx / 000 (no connection)    -> FAIL  (real bug / service down — this is what you fix)
#   other                        -> WARN
# ---------------------------------------------------------------------------
check() {
  local name="$1" method="$2" url="$3" body="${4:-}"
  local tmp; tmp=$(mktemp)
  local code time_total curl_err=0

  if [[ -n "$body" ]]; then
    resp=$(curl -sS -m "$TIMEOUT" -o "$tmp" -w "%{http_code}|%{time_total}" \
      -X "$method" -H "Content-Type: application/json" -d "$body" "$url" 2>>"$LOGFILE") || curl_err=1
  else
    resp=$(curl -sS -m "$TIMEOUT" -o "$tmp" -w "%{http_code}|%{time_total}" \
      -X "$method" "$url" 2>>"$LOGFILE") || curl_err=1
  fi

  code="${resp%%|*}"
  time_total="${resp##*|}"
  [[ -z "$code" ]] && code="000"

  local snippet
  snippet=$(head -c 220 "$tmp" 2>/dev/null | tr -d '\n')
  rm -f "$tmp"

  local status label
  if [[ "$curl_err" -eq 1 || "$code" == "000" ]]; then
    status="FAIL"; label="${C_R}FAIL${C_N}"; FAIL=$((FAIL+1))
  elif [[ "$code" =~ ^2[0-9][0-9]$ ]]; then
    status="PASS"; label="${C_G}PASS${C_N}"; PASS=$((PASS+1))
  elif [[ "$code" =~ ^(400|401|403|404|405|422|429)$ ]]; then
    status="WARN"; label="${C_Y}WARN${C_N}"; WARN=$((WARN+1))
  elif [[ "$code" =~ ^5[0-9][0-9]$ ]]; then
    status="FAIL"; label="${C_R}FAIL${C_N}"; FAIL=$((FAIL+1))
  else
    status="WARN"; label="${C_Y}WARN${C_N}"; WARN=$((WARN+1))
  fi

  printf "%-6b %-45s %-7s %-8s %6ss  %s\n" "$label" "$name" "$method" "$code" "$time_total" "$url" | tee -a "$LOGFILE" >/dev/null
  printf "%-6s %-45s %-7s %-8s %6ss  %s\n" "$status" "$name" "$method" "$code" "$time_total" "$url"

  if [[ "$status" == "FAIL" ]]; then
    {
      echo "----- FAIL: $name -----"
      echo "  $method $url"
      echo "  http_code=$code time=${time_total}s"
      echo "  body_snippet: $snippet"
      echo
    } >> "$FAILFILE"
  fi
}

skip() { log "${C_Y}SKIP${C_N}   $1 (reason: $2)"; SKIP=$((SKIP+1)); }

wait_for() {
  local name="$1" url="$2"
  local waited=0
  printf "waiting for %-30s" "$name..."
  while true; do
    code=$(curl -s -o /dev/null -m 5 -w "%{http_code}" "$url" 2>/dev/null)
    if [[ "$code" =~ ^2|3 ]]; then
      echo -e " ${C_G}up${C_N} (${waited}s)"
      return 0
    fi
    if [[ $waited -ge $STARTUP_WAIT ]]; then
      echo -e " ${C_R}NOT RESPONDING after ${STARTUP_WAIT}s (last code: $code)${C_N}"
      return 1
    fi
    sleep 3
    waited=$((waited+3))
  done
}

###############################################################################
log "${C_BOLD}Stockky full-stack test sweep — $(date)${C_N}"
log "Log file: $LOGFILE"
log "Failures file: $FAILFILE"
log "with-writes: $WITH_WRITES | project-dir (auto/given): $PROJECT_DIR | host private IP: $HOST_IP_PRIVATE"
log "Targets: gateway=$GATEWAY_URL market=$MARKET_URL analysis=$ANALYSIS_URL decision=$DECISION_URL notif=$NOTIF_URL trade=$TRADE_URL frontend=$FRONTEND_URL"

###############################################################################
sect "STEP 0 — Python syntax / import compile check (catches syntax errors)"
if [[ -d "$PROJECT_DIR" ]]; then
  mapfile -t PYFILES < <(find "$PROJECT_DIR" -name "*.py" -not -path "*/node_modules/*" -not -path "*/.venv/*" 2>/dev/null)
  NPY=${#PYFILES[@]}
  log "Compiling $NPY .py files under $PROJECT_DIR ..."
  SYNTAX_ERR=0
  if [[ $NPY -gt 0 ]]; then
    for f in "${PYFILES[@]}"; do
      out=$(python3 -m py_compile "$f" 2>&1)
      if [[ -n "$out" ]]; then
        SYNTAX_ERR=$((SYNTAX_ERR+1))
        log "${C_R}SYNTAX ERROR${C_N} in $f"
        echo "$out" | tee -a "$LOGFILE" >> "$FAILFILE"
      fi
    done
  fi
  if [[ $NPY -eq 0 ]]; then
    skip "python-syntax-check" "no .py files found under $PROJECT_DIR — is PROJECT_DIR correct?"
  elif [[ $SYNTAX_ERR -eq 0 ]]; then
    log "${C_G}✔ No Python syntax errors found${C_N}"
  else
    log "${C_R}✘ $SYNTAX_ERR file(s) with syntax errors — see $FAILFILE${C_N}"
    FAIL=$((FAIL+SYNTAX_ERR))
  fi
else
  skip "python-syntax-check" "PROJECT_DIR '$PROJECT_DIR' not found on this machine — pass --project-dir /path/to/stockky-v2-main"
fi

###############################################################################
sect "STEP 1 — Service reachability"
wait_for "market-data-service"          "$MARKET_URL/health"
wait_for "analysis-intelligence-service" "$ANALYSIS_URL/health"
wait_for "decision-prediction-service"   "$DECISION_URL/health"
wait_for "api-gateway"                   "$GATEWAY_URL/health"
wait_for "notification-scheduler-service" "$NOTIF_URL/health"
wait_for "real-trade-service"            "$TRADE_URL/health"
wait_for "frontend"                      "$FRONTEND_URL/"

###############################################################################
sect "STEP 2 — market-data-service ($MARKET_URL)"
check "root"                    GET "$MARKET_URL/"
check "health"                  GET "$MARKET_URL/health"
check "bhavcopy universe"       GET "$MARKET_URL/bhavcopy/universe"
check "live-quote/$SYM"         GET "$MARKET_URL/live-quote/$SYM"
check "yahoo-ws-status"         GET "$MARKET_URL/internal/yahoo-ws-status"
check "quote/$SYM"              GET "$MARKET_URL/quote/$SYM"
check "quote/$SYM2"             GET "$MARKET_URL/quote/$SYM2"
check "history/$SYM"            GET "$MARKET_URL/history/$SYM"
check "fundamentals/$SYM"       GET "$MARKET_URL/fundamentals/$SYM"
check "surprise/premarket"      GET "$MARKET_URL/surprise/premarket"
check "surprise/premarket/status" GET "$MARKET_URL/surprise/premarket/status"
check "surprise/static"         GET "$MARKET_URL/surprise/static"
check "delivery/$SYM"           GET "$MARKET_URL/delivery/$SYM"

###############################################################################
sect "STEP 3 — analysis-intelligence-service ($ANALYSIS_URL)"
check "root"                    GET "$ANALYSIS_URL/"
check "health (mount map)"      GET "$ANALYSIS_URL/health"
check "technical/health"        GET "$ANALYSIS_URL/technical/health"
check "technical/analyze/$SYM"  GET "$ANALYSIS_URL/technical/analyze/$SYM"
check "technical/sector-strength/$SYM" GET "$ANALYSIS_URL/technical/sector-strength/$SYM"
check "fundamental/health"      GET "$ANALYSIS_URL/fundamental/health"
check "fundamental/analyze/$SYM" GET "$ANALYSIS_URL/fundamental/analyze/$SYM"
check "news/health"             GET "$ANALYSIS_URL/news/health"
check "news/analyze/$SYM"       GET "$ANALYSIS_URL/news/analyze/$SYM"
check "event/health"            GET "$ANALYSIS_URL/event/health"
check "event/events/$SYM"       GET "$ANALYSIS_URL/event/events/$SYM"
check "event/events/$SYM/categorized" GET "$ANALYSIS_URL/event/events/$SYM/categorized"
check "event/symbols_with_events" GET "$ANALYSIS_URL/event/symbols_with_events"
check "sentiment/health"        GET "$ANALYSIS_URL/sentiment/health"
check "sentiment/sentiment"     GET "$ANALYSIS_URL/sentiment/sentiment"

###############################################################################
sect "STEP 4 — decision-prediction-service ($DECISION_URL)"
check "root"                    GET "$DECISION_URL/"
check "health (mount map)"      GET "$DECISION_URL/health"
check "decision/health"         GET "$DECISION_URL/decision/health"
check "decision/circuits"       GET "$DECISION_URL/decision/circuits"
check "decision/decide/$SYM"    GET "$DECISION_URL/decision/decide/$SYM"
check "prediction/health"       GET "$DECISION_URL/prediction/health"
check "prediction/model/info"   GET "$DECISION_URL/prediction/model/info"
check "prediction/predict/$SYM" GET "$DECISION_URL/prediction/predict/$SYM"
check "training/health"         GET "$DECISION_URL/training/health"
check "training/api/status"     GET "$DECISION_URL/training/api/status"
check "training/api/report"     GET "$DECISION_URL/training/api/report"
check "training/api/models"     GET "$DECISION_URL/training/api/models"
check "training/api/insights"   GET "$DECISION_URL/training/api/insights"
check "training/api/metrics/summary" GET "$DECISION_URL/training/api/metrics/summary"
check "training/train/status"   GET "$DECISION_URL/training/train/status"
check "training/model-status"   GET "$DECISION_URL/training/model-status"
check "training/training-score/$SYM" GET "$DECISION_URL/training/training-score/$SYM"
check "training/lock-status"    GET "$DECISION_URL/training/lock-status"
check "training/api/portfolio/summary" GET "$DECISION_URL/training/api/portfolio/summary"
check "training/api/trades"     GET "$DECISION_URL/training/api/trades"
check "training/api/trades/summary" GET "$DECISION_URL/training/api/trades/summary"

###############################################################################
sect "STEP 5 — api-gateway ($GATEWAY_URL) [read-only]"
check "root"                    GET "$GATEWAY_URL/"
check "health"                  GET "$GATEWAY_URL/health"
check "ready"                   GET "$GATEWAY_URL/ready"
check "system/health"           GET "$GATEWAY_URL/system/health"
check "quote/$SYM"              GET "$GATEWAY_URL/quote/$SYM"
check "market/history/$SYM"     GET "$GATEWAY_URL/market/history/$SYM"
check "circuits"                GET "$GATEWAY_URL/circuits"
check "ops/rate-limits"         GET "$GATEWAY_URL/ops/rate-limits"
check "metrics"                 GET "$GATEWAY_URL/metrics"
check "ops/db-status"           GET "$GATEWAY_URL/ops/db-status"
check "ops/circuit-status"      GET "$GATEWAY_URL/ops/circuit-status"
check "watchlist"               GET "$GATEWAY_URL/watchlist"
check "events/$SYM"             GET "$GATEWAY_URL/events/$SYM"
check "stock/$SYM"              GET "$GATEWAY_URL/stock/$SYM"
check "scan/last"               GET "$GATEWAY_URL/scan/last"
check "scan/watchlist"          GET "$GATEWAY_URL/scan/watchlist"
check "market/session"          GET "$GATEWAY_URL/market/session"
check "market/top-gainers"      GET "$GATEWAY_URL/market/top-gainers"
check "market/top-losers"       GET "$GATEWAY_URL/market/top-losers"
check "market/most-active"      GET "$GATEWAY_URL/market/most-active"
check "market/trending"         GET "$GATEWAY_URL/market/trending"
check "market/indices"          GET "$GATEWAY_URL/market/indices"
check "scan/universe"           GET "$GATEWAY_URL/scan/universe"
check "universe"                GET "$GATEWAY_URL/universe"
check "notifications/health"    GET "$GATEWAY_URL/notifications/health"
check "notifications/config"    GET "$GATEWAY_URL/notifications/config"
check "training/status"         GET "$GATEWAY_URL/training/status"
check "training/score/$SYM"     GET "$GATEWAY_URL/training/score/$SYM"
check "api/surprise/audit"      GET "$GATEWAY_URL/api/surprise/audit"
check "api/surprise/static"     GET "$GATEWAY_URL/api/surprise/static"
check "ipo/status"               GET "$GATEWAY_URL/ipo/status"
check "ipo/list"                 GET "$GATEWAY_URL/ipo/list"
check "ipo/audit"                GET "$GATEWAY_URL/ipo/audit"
check "surprise/premarket/status" GET "$GATEWAY_URL/surprise/premarket/status"
check "stockky-hot"              GET "$GATEWAY_URL/stockky-hot"
check "stockky-hot/status"       GET "$GATEWAY_URL/stockky-hot/status"
check "stockky-hot/result"       GET "$GATEWAY_URL/stockky-hot/result"
check "stockky-hot/table"        GET "$GATEWAY_URL/stockky-hot/table"
check "stockky-hot/audit"        GET "$GATEWAY_URL/stockky-hot/audit"
check "catalysts/alert/status"   GET "$GATEWAY_URL/catalysts/alert/status"
check "api/quotes/bulk-cache"    GET "$GATEWAY_URL/api/quotes/bulk-cache"
check "price-alerts"             GET "$GATEWAY_URL/price-alerts"
check "data-feed/refill-additional/status" GET "$GATEWAY_URL/data-feed/refill-additional/status"
check "data-feed/meta"           GET "$GATEWAY_URL/data-feed/meta"
check "data-feed/status"         GET "$GATEWAY_URL/data-feed/status"
check "data-feed/audit-missing"  GET "$GATEWAY_URL/api/feed/audit-missing"
check "data-feed/$SYM"           GET "$GATEWAY_URL/data-feed/$SYM"

###############################################################################
sect "STEP 6 — notification-scheduler-service ($NOTIF_URL) [read-only]"
check "root"                    GET "$NOTIF_URL/"
check "health"                  GET "$NOTIF_URL/health"
check "notification/root"       GET "$NOTIF_URL/notification/"
check "notification/health"     GET "$NOTIF_URL/notification/health"
check "notification/config"     GET "$NOTIF_URL/notification/config"
check "notification/outbox"     GET "$NOTIF_URL/notification/outbox"
check "notification/call/me"    GET "$NOTIF_URL/notification/call/me"
check "scheduler/health"        GET "$NOTIF_URL/scheduler/health"
check "scheduler/hydrate/weekend" GET "$NOTIF_URL/scheduler/hydrate/weekend"

###############################################################################
sect "STEP 7 — real-trade-service ($TRADE_URL) [read-only, DEMO mode only — no orders/arming touched]"
check "health"                  GET "$TRADE_URL/health"
check "status/$MODE"            GET "$TRADE_URL/status/$MODE"
check "dhan/status"             GET "$TRADE_URL/dhan/status"
check "dhan/network-check"      GET "$TRADE_URL/dhan/network-check"
check "pipeline/status/$MODE"   GET "$TRADE_URL/pipeline/status/$MODE"
check "positions/$MODE"         GET "$TRADE_URL/positions/$MODE"
check "orders/$MODE"            GET "$TRADE_URL/orders/$MODE"
check "adaptive/status"         GET "$TRADE_URL/adaptive/status"
check "candidates/$MODE"        GET "$TRADE_URL/candidates/$MODE"
check "audit-log"               GET "$TRADE_URL/audit-log"
log "${C_Y}NOTE: /dhan/positions,/dhan/holdings,/dhan/orders require an authenticated admin session — 401/403 here is expected, not a bug.${C_N}"
check "dhan/positions"          GET "$TRADE_URL/dhan/positions"
check "dhan/holdings"           GET "$TRADE_URL/dhan/holdings"
check "dhan/orders"             GET "$TRADE_URL/dhan/orders"

###############################################################################
sect "STEP 8 — frontend ($FRONTEND_URL)"
check "frontend served"         GET "$FRONTEND_URL/"

###############################################################################
if [[ "$WITH_WRITES" -eq 1 ]]; then
  sect "STEP 9 — optional writes (--with-writes): safe/reversible only"
  TESTSYM="ZZZTESTSTOCK"
  check "watchlist/add ($TESTSYM)" POST "$GATEWAY_URL/watchlist/add" "{\"symbols\":[\"$TESTSYM\"]}"
  check "watchlist (after add)"    GET  "$GATEWAY_URL/watchlist"
  check "watchlist/remove ($TESTSYM)" DELETE "$GATEWAY_URL/watchlist/$TESTSYM"
  check "watchlist (after remove)" GET  "$GATEWAY_URL/watchlist"
  log "${C_Y}Sending a REAL test notification to whatever channels you have configured (Telegram/Discord/Slack/CallMeBot)...${C_N}"
  check "notifications/test (fires real alert)" POST "$GATEWAY_URL/notifications/test"
else
  sect "STEP 9 — optional writes SKIPPED (run with --with-writes to include watchlist roundtrip + a real notification test ping)"
fi

###############################################################################
sect "NOT AUTO-TESTED — review manually (money-risk / destructive / heavy / external-spam)"
cat <<'EOF' | tee -a "$LOGFILE"
real-trade-service (money risk — test manually, ideally in DHAN_ENV=sandbox first):
  POST /auth/login, /auth/logout, /dhan/connect
  POST /risk-config, /risk-config/{mode}/confirm
  POST /arm/{mode}, /disarm/{mode}, /emergency-pause
  POST /manual-order/{mode}/preview|confirm, /candidates/manual/{mode}
  POST /cycle/run/{mode}, /autopilot/{mode}/enable|disable
  POST /positions/{mode}/{id}/close, /orders/{mode}/{id}/cancel, /reconcile/{mode}

api-gateway — heavy/slow (full universe) or state-mutating:
  GET/POST /scan, /scan/start, /scan/batch, /api/scan/find-buys, /scan/stream
  POST /data-feed/hard-reset, /data-feed/start-bulk-feed, /data-feed/run, /data-feed/resume, /data-feed/stop
  POST /api/feed/repair-*, /api/feed/purge-over-cap
  DELETE /scan/universe/cache
  POST /ops/power-off, /ops/resume-activity
  POST /surprise/*, /ipo/scan|stop|add|repair-batch, /stockky-hot/run|stop
  POST /price-alerts (create), DELETE /price-alerts/{id}
  POST /notifications/config, /notifications/send-picks

decision-prediction-service — mutates training DB / long-running:
  POST /training/api/train, /train/run, /train, /training/train/clear-lock
  POST /training/api/evaluate/t1|t5, /training/api/actionable/commit
  POST /training/api/trades/manual, /training/api/trades/{id}/close|add
  POST /decision/decide/evaluate, /decision/decide/batch

notification-scheduler-service:
  POST /notification/notify, /notification/outbox/process, /notification/call/me
  (POST /notification/test is covered by --with-writes above)
EOF

###############################################################################
END_TS=$(date +%s)
ELAPSED=$((END_TS-START_TS))
sect "SUMMARY"
log "PASS: ${C_G}${PASS}${C_N}   WARN: ${C_Y}${WARN}${C_N}   FAIL: ${C_R}${FAIL}${C_N}   SKIP: ${C_Y}${SKIP}${C_N}"
log "Elapsed: ${ELAPSED}s (~$((ELAPSED/60))m $((ELAPSED%60))s)"
log "Full log:      $LOGFILE"
log "Failures only: $FAILFILE"
if [[ "$FAIL" -gt 0 ]]; then
  log "${C_R}${FAIL} FAILURE(S) found — send $FAILFILE (and $LOGFILE if needed) back for fixes.${C_N}"
  exit 1
else
  log "${C_G}No hard failures. Check WARN lines for anything that should have returned data but returned 4xx.${C_N}"
  exit 0
fi
