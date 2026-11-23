#!/usr/bin/env bash
# Round 676 (P0.1 of the 23 Nov plan): the daily blocked-name read in ONE command. Run from ~/stockky-v2 after 15:45 IST:
#   bash scripts/gates_daily.sh [YYYY-MM-DD]        (default = today, IST)
# For the gates no_setup_window and structure it runs scripts/blocked_sources.py (real-trade container), takes the ready --syms line and runs
# scripts/gate_outcome_dhan.py (market-data container) on it, 25 names per call with a 2 s pause (Dhan answers HTTP 429 when asked too fast).
# Then the recorded-bars read of the structure gate (scripts/blocked_outcome.py). Read-only; writes nothing. GATES="structure" limits it, CHUNK=15 shrinks a call.
set -u
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "$0")/.." || exit 1
DAY="${1:-$(TZ=Asia/Kolkata date +%F)}"
GATES="${GATES:-no_setup_window structure}"
CHUNK="${CHUNK:-25}"
if [[ "$DAY" > "$(TZ=Asia/Kolkata date +%F)" ]]; then   # round 678: a future day has no blocked rows yet
  echo "DAY=$DAY is after today ($(TZ=Asia/Kolkata date +%F) IST): nothing to read yet. Run it on the day itself after 15:45 IST." >&2
  exit 2
fi
if [ "$DAY" = "$(TZ=Asia/Kolkata date +%F)" ] && [ "$(TZ=Asia/Kolkata date +%H%M)" -lt 1545 ]; then
  echo "WARNING: it is before 15:45 IST, so the day is not finished; later prices are missing from this read." >&2
fi
# round 679: the read is saved to gates_out/gates_<DAY>.txt and scripts/gate_rule_summary.py applies the P2 rule to every saved day (it needs 3 sessions and 60 names before
# it can ever say CANDIDATE). A read taken before 15:45 IST is saved as gates_out/partial_<DAY>.txt, which the summary does not count as a session.
if [ -z "${GATES_DAILY_INNER:-}" ]; then
  mkdir -p gates_out
  f="gates_out/gates_${DAY}.txt"
  if [ "$DAY" = "$(TZ=Asia/Kolkata date +%F)" ] && [ "$(TZ=Asia/Kolkata date +%H%M)" -lt 1545 ]; then f="gates_out/partial_${DAY}.txt"; fi
  GATES_DAILY_INNER=1 GATES="$GATES" CHUNK="$CHUNK" bash "$SELF" "$DAY" 2>&1 | tee "$f"
  echo; echo "saved: $f"
  if ls gates_out/gates_*.txt >/dev/null 2>&1; then echo; python3 scripts/gate_rule_summary.py; else echo "(no finished session saved yet, so no rule check: run it after 15:45 IST)"; fi
  exit 0
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
