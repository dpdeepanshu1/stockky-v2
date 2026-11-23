#!/usr/bin/env bash
# Round 676 (P0.1 of the 23 Nov plan): the daily blocked-name read in ONE command. Run from ~/stockky-v2 after 15:45 IST:
#   bash scripts/gates_daily.sh [YYYY-MM-DD]        (default = today, IST)
# For the gates no_setup_window and structure it runs scripts/blocked_sources.py (real-trade container), takes the ready --syms line and runs
# scripts/gate_outcome_dhan.py (market-data container) on it, 25 names per call with a 2 s pause (Dhan answers HTTP 429 when asked too fast).
# Then the recorded-bars read of the structure gate (scripts/blocked_outcome.py). Read-only; writes nothing. GATES="structure" limits it, CHUNK=15 shrinks a call.
set -u
cd "$(dirname "$0")/.." || exit 1
DAY="${1:-$(TZ=Asia/Kolkata date +%F)}"
GATES="${GATES:-no_setup_window structure}"
CHUNK="${CHUNK:-25}"
if [ "$DAY" = "$(TZ=Asia/Kolkata date +%F)" ] && [ "$(TZ=Asia/Kolkata date +%H%M)" -lt 1545 ]; then
  echo "WARNING: it is before 15:45 IST, so the day is not finished; later prices are missing from this read." >&2
fi
for g in $GATES; do
  echo; echo "################ gate $g  day $DAY ################"
  out=$(docker compose exec -T real-trade-service python - --day "$DAY" --gate "$g" < scripts/blocked_sources.py 2>&1)
  echo "$out"
  syms=$(printf '%s\n' "$out" | grep '^--syms ' | head -1 | sed -e 's/^--syms "//' -e 's/"$//')
  if [ -z "$syms" ]; then echo "(no names for $g: nothing to price)"; continue; fi
  mapfile -t chunks < <(printf '%s\n' $syms | xargs -n "$CHUNK")
  i=0
  for c in "${chunks[@]}"; do
    i=$((i + 1)); echo; echo "---- Dhan outcome, part $i of ${#chunks[@]} ----"
    docker compose exec -T market-data-service python - --day "$DAY" --pause 2 --syms "$c" < scripts/gate_outcome_dhan.py 2>&1
  done
done
echo; echo "################ structure gate, recorded bars ################"
docker compose exec -T real-trade-service python - --day "$DAY" --gate structure < scripts/blocked_outcome.py 2>&1
