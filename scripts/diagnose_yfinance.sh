#!/usr/bin/env bash
# diagnose_yfinance.sh - one-shot report for log-audit item 6 (yfinance returns nothing on the VM).
# Usage (from the repo root on the VM, e.g. ~/stockky-v2):   bash scripts/diagnose_yfinance.sh 2>&1 | tee yf_report.txt
# Read-only: makes a few GET requests to Yahoo, runs one yfinance probe inside the api-gateway container and
# greps recent logs. Changes nothing, prints no secrets. Paste yf_report.txt back.
set -u
cd "$(dirname "$0")/.." || exit 1
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SINCE="${SINCE:-3h}"

echo "== time: $(date -u '+%Y-%m-%d %H:%M:%S') UTC"

echo; echo "== 1. DNS"
for h in query1.finance.yahoo.com query2.finance.yahoo.com fc.yahoo.com finance.yahoo.com; do
  printf '%-28s %s\n' "$h" "$(getent hosts "$h" | head -1 | awk '{print $1}')"
done

probe() {  # label, url, extra curl args...
  local label="$1" url="$2"; shift 2
  local out code secs
  out=$(curl -s -o /tmp/yf_body.$$ -w '%{http_code} %{time_total}' --max-time 20 "$@" "$url" 2>&1) || true
  code=${out%% *}; secs=${out##* }
  printf '%-34s http=%s time=%ss' "$label" "$code" "$secs"
  if [ "$code" != "200" ]; then printf '  body: %s' "$(head -c 140 /tmp/yf_body.$$ 2>/dev/null | tr '\n' ' ')"; fi
  echo
  rm -f /tmp/yf_body.$$
}

echo; echo "== 2. Yahoo chart endpoint from the VM (default curl UA vs browser UA)"
C1="https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1m"
C2="https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1m"
probe "query1 default UA"  "$C1"
probe "query1 browser UA"  "$C1" -A "$UA"
probe "query2 browser UA"  "$C2" -A "$UA"
probe "fc.yahoo.com browser UA" "https://fc.yahoo.com" -A "$UA"

echo; echo "== 3. yfinance inside the api-gateway container"
if command -v docker >/dev/null 2>&1; then
  docker compose exec -T api-gateway python3 - <<'PY' 2>&1 | tail -n 25
import time
try:
    import yfinance as yf
    print("yfinance", getattr(yf, "__version__", "?"))
except Exception as e:
    print("import failed:", type(e).__name__, str(e)[:160]); raise SystemExit
for sym in ("RELIANCE.NS", "TCS.NS", "^NSEI"):
    t = time.time()
    try:
        h = yf.Ticker(sym).history(period="1d", interval="1m")
        print(f"{sym:12s} rows={len(h)} secs={time.time()-t:.1f}")
    except Exception as e:
        print(f"{sym:12s} ERR {type(e).__name__}: {str(e)[:160]}")
PY
else
  echo "docker not found - run this on the VM, from the repo root"
fi

echo; echo "== 4. Recent yfinance-related log lines (last $SINCE, newest 40)"
if command -v docker >/dev/null 2>&1; then
  docker compose logs --since "$SINCE" api-gateway market-data-service 2>&1 \
    | grep -iE "yfinance|yahoo|Too Many Requests|429|rate.?limit|Movers:|YFRateLimit|curl_cffi|JSONDecode|delisted" \
    | tail -n 40 | cut -c1-260
fi

echo; echo "== done. Paste this whole report back."
