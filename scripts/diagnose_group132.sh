#!/usr/bin/env bash
# diagnose_group132.sh - one-shot report for the two open items after group 132:
#   A. does the closed-market boot restore (surprise last result) work, and did the quote burst go away?
#   B. why do HFCL / MTARTECH / STLTECH (and the other "unresolved" names) not resolve on AngelOne?
# Usage (repo root on the VM, e.g. ~/stockky-v2):   bash scripts/diagnose_group132.sh 2>&1 | tee g132_report.txt
#   Extra names:  NAMES="HFCL FOO" bash scripts/diagnose_group132.sh
# Read-only: one GET of AngelOne's public scrip-master file inside market-data-service, one read of the
# stockky_kv row inside api-gateway, and log greps. Changes nothing, prints no secrets. Paste g132_report.txt back.
set -u
cd "$(dirname "$0")/.." || exit 1
SINCE="${SINCE:-24h}"
NAMES="${NAMES:-}"
command -v docker >/dev/null 2>&1 || { echo "docker not found - run this on the VM, from the repo root"; exit 1; }

echo "== time: $(date -u '+%Y-%m-%d %H:%M:%S') UTC (IST: $(TZ=Asia/Kolkata date '+%a %H:%M'))"

echo; echo "== A1. api-gateway boot: restore / pre-warm lines (last $SINCE)"
docker compose logs --since "$SINCE" api-gateway 2>&1 \
  | grep -E "Startup: (market closed|surprise/scan cache pre-warmed)|Startup warning \(surprise-scan" \
  | cut -c1-240 | tail -n 10
echo "(want after the SECOND closed-market restart: 'restored the last surprise/scan result, skipped the boot quote sweep')"

echo; echo "== A2. /quote calls market-data-service served since api-gateway last started"
STARTED=$(docker inspect -f '{{.State.StartedAt}}' "$(docker compose ps -q api-gateway | head -1)" 2>/dev/null)
echo "api-gateway StartedAt: ${STARTED:-unknown}"
if [ -n "${STARTED:-}" ]; then
  N=$(docker compose logs --since "$STARTED" market-data-service 2>&1 | grep -cE '"GET /quote/')
  echo "GET /quote/ lines since then: $N   (a full sweep is ~1000; a few dozen is normal background traffic)"
fi

echo; echo "== A3. durable last-result row (inside api-gateway)"
docker compose exec -T api-gateway python3 - < scripts/diagnose_g132_row.py 2>&1 | grep -v "^INFO" | tail -n 12

echo; echo "== B1. names AngelOne could not resolve (market-data-service log, last $SINCE)"
docker compose logs --since "$SINCE" market-data-service 2>&1 | grep -E "resolved [0-9]+/[0-9]+ requested symbols" | tail -n 2 | cut -c1-400

echo; echo "== B2. those names in AngelOne's scrip master (series / segment)"
# shellcheck disable=SC2086
docker compose exec -T market-data-service python3 - $NAMES < scripts/diagnose_g132_scrip.py 2>&1 | tail -n 60

echo; echo "== done. Paste this whole report back."
