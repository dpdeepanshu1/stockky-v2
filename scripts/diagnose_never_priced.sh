#!/usr/bin/env bash
# diagnose_never_priced.sh - one-shot report for "symbols that never price" (STEAMHOUSE, SGRL, KENNAMET ...):
#   1. which NSE series / segment does AngelOne list them under (EQ, BE, BZ, SM = SME ...)?  A name that only has an -SM
#      row is an SME-board stock: it is not in the EQ/BE/BZ bhavcopy (bhavcopy.process_bhavcopy_rows drops other series),
#      so /last-close answers 404 and the scan can never get a price from the bhavcopy tier.
#   2. what market-data-service answers for /quote and /last-close, and how long it takes (waterfall vs negative cache).
#   3. which service puts them in front of real-trade: log lines from market-data, real-trade and api-gateway.
# Usage (repo root on the VM):   bash scripts/diagnose_never_priced.sh 2>&1 | tee never_priced_report.txt
#   Other names:  NAMES="FOO BAR" bash scripts/diagnose_never_priced.sh      Log window:  SINCE=6h
# Read-only: one GET of AngelOne's public scrip-master file and a few localhost GETs inside market-data-service, plus log
# greps. Changes nothing, prints no secrets. Paste never_priced_report.txt back.
set -u
cd "$(dirname "$0")/.." || exit 1
SINCE="${SINCE:-24h}"
NAMES="${NAMES:-STEAMHOUSE SGRL KENNAMET ELEVATE ROSSTECH}"
command -v docker >/dev/null 2>&1 || { echo "docker not found - run this on the VM, from the repo root"; exit 1; }
PAT=$(echo "$NAMES" | tr -s ' ' '|')

echo "== time: $(date -u '+%Y-%m-%d %H:%M:%S') UTC (IST: $(TZ=Asia/Kolkata date '+%a %H:%M'))   names: $NAMES"

echo; echo "== 1. AngelOne scrip master rows (series suffix visible: -EQ, -BE, -BZ, -SM ...)"
# shellcheck disable=SC2086
docker compose exec -T market-data-service python3 - $NAMES < scripts/diagnose_g132_scrip.py 2>&1 | tail -n 80

echo; echo "== 2. market-data-service /quote and /last-close, timed"
# shellcheck disable=SC2086
docker compose exec -T market-data-service python3 - $NAMES < scripts/diagnose_never_priced.py 2>&1 | tail -n 60

echo; echo "== 3a. market-data-service log lines naming them (last $SINCE)"
docker compose logs --since "$SINCE" market-data-service 2>&1 | grep -E "($PAT)" | cut -c1-220 | tail -n 25

echo; echo "== 3b. real-trade-service log lines naming them (last $SINCE): pauses, timeouts, rejections"
docker compose logs --since "$SINCE" real-trade-service 2>&1 | grep -E "($PAT)" | cut -c1-220 | tail -n 25

echo; echo "== 3c. api-gateway: are they in the scan universe / movers right now?"
docker compose exec -T api-gateway python3 - $NAMES <<'PY' 2>&1 | tail -n 20
import json, sys, urllib.request
names = [a.upper() for a in sys.argv[1:]]
try:
    with urllib.request.urlopen("http://127.0.0.1:8000/scan/universe?cached=true", timeout=60) as r:
        d = json.load(r)
except Exception as e:
    print("scan/universe failed:", type(e).__name__, str(e)[:160]); raise SystemExit
syms, movers = set(d.get("symbols") or []), set(d.get("momentum_movers") or [])
print("universe size", d.get("total"), "| movers", len(movers), "| stale" if d.get("stale") else "| fresh")
for n in names:
    print("  %-12s in universe: %-3s  in momentum movers: %s" % (n, "YES" if n in syms else "no", "YES" if n in movers else "no"))
PY

echo; echo "== done. Paste this whole report back."
