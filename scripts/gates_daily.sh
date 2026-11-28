#!/usr/bin/env bash
# Round 676 (P0.1 of the 23 Nov plan): the daily blocked-name read in ONE command. Run from ~/stockky-v2 after 15:45 IST:
#   bash scripts/gates_daily.sh [YYYY-MM-DD]        (default = today, IST)
# For every gate in GATES (default: the 14 tags set in GATES= below) it runs scripts/blocked_sources.py (real-trade container), takes the ready --syms line and runs
# scripts/gate_outcome_dhan.py (market-data container) on it, 25 names per call with a 2 s pause (Dhan answers HTTP 429 when asked too fast).
# Then the recorded-bars read of the structure gate (scripts/blocked_outcome.py). Read-only; writes nothing. GATES="structure" limits it, CHUNK=15 shrinks a call.
set -u
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "$0")/.." || exit 1
DAY="${1:-$(TZ=Asia/Kolkata date +%F)}"
# round 685: the 23 Nov WAIT rows were blocked by SEVEN tags but only two were ever priced; the other five (time_machine 226 rows, order_flow 154, liquidity 130,
# vwap_below 23, structure_nodata 46) had no evidence either way. gate_rule_summary.py already groups by whatever gate header it finds, so no other script changes.
# round 689: five more WAIT paths used to be written with NO gate tag (market regime, regime rules, price drift, the 1:2 reward:risk floor, the one-share risk cap), so none of them could be priced
# here. They carry a tag now (entry_engine/entry.py); a day's read only finds them from the first session after the Real Trade service is rebuilt with round 689.
# round 690: the last three untagged WAIT paths (not_actionable, vs_base_off, bad_stop) and no_quote / stale_price are tagged too but left OUT of GATES on purpose: they are a label, a config switch, a bad stop and data-feed health, not entry judgements a price read could loosen.
# round 691: two more judgement WAITs were written with no tag because they set decision.action directly instead of calling _wait(): a risk-approved pick below the composite quality floor
# (below_floor) and one that cleared the floor but lost the top-N ranking (not_top_n). Both are in GATES now; dup_staged and auto_pause_reserve are bookkeeping and stay out.
GATES="${GATES:-no_setup_window structure time_machine order_flow liquidity vwap_below structure_nodata regime regime_rules entry_drift rr_floor risk_cap below_floor not_top_n}"
CHUNK="${CHUNK:-25}"
if [[ "$DAY" > "$(TZ=Asia/Kolkata date +%F)" ]]; then   # round 678: a future day has no blocked rows yet
  echo "DAY=$DAY is after today ($(TZ=Asia/Kolkata date +%F) IST): nothing to read yet. Run it on the day itself after 15:45 IST." >&2
  exit 2
fi
# round 683: an NSE holiday has no REAL rows, and a saved empty read adds a file to the rule check without adding a session (24 Nov 2026, Guru Nanak Jayanti, was one).
# round 702: the round 683 guard skipped Saturdays and Sundays (`date +%u` < 6), so a weekend run (Sat 28 Nov 2026) walked all 14 gates, printed "no rows" for each and saved an empty
# gates_out/gates_<DAY>.txt. A weekend is now refused the same way as a holiday (exit 3, nothing saved).
if [ -z "${GATES_DAILY_INNER:-}" ]; then
  if [ "$(date -d "$DAY" +%u)" -ge 6 ]; then
    echo "DAY=$DAY is a $(date -d "$DAY" +%A): the market is closed, no session, nothing saved. Pass the last trading day, e.g. bash scripts/gates_daily.sh $(date -d "$DAY -$(( $(date -d "$DAY" +%u) - 5 )) day" +%F)" >&2
    exit 3
  fi
  if grep -E "^\s+\"$DAY\"," services/real-trade-service/tz_utils.py >/dev/null 2>&1; then
    echo "DAY=$DAY is an NSE holiday in services/real-trade-service/tz_utils.py: no session, nothing saved." >&2
    exit 3
  fi
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
