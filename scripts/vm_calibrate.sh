#!/usr/bin/env bash
# ============================================================
# Stockky structure calibration + G1 + board-parity run (group405). Run on the VM (it needs the market-data host and, for the
# parity step, the position-stocks service). Read-only: downloads daily candles, runs the existing offline tools, writes files.
#
#   MARKET_DATA_URL=http://localhost:8001 API_GATEWAY_URL=http://localhost:8000 bash scripts/vm_calibrate.sh
#
# Env: SEEDS="7 11 13"   one random sample per seed (same seed = same symbols); the A/B verdict must hold on every one
#      LIMIT=400         symbols per sample (lower it, e.g. 150, if the market-data host rate limits)
#      JOBS=N            parallel workers for the A/B replay (default: all CPUs);  smaller LIMIT / larger STEP = faster
#      SYMBOLS=A,B,..    use these instead of the universe;  OUT_DIR=./calibration_out;  PERIOD=5y;  STEP=3;  HORIZON=10
#      LIVE_REPORT_URL   GET /api/structure/report of decision-prediction (adds the G4 live check)
#      PS_STATUS_URL     http://localhost:8006/board/status (adds the legacy-removal parity verdict); PS_TOKEN if it needs a bearer token
#      DRY_RUN=1         print the commands and run nothing;  PYTHON_BIN=python3
# Steps: fetch history per seed -> ms_ab_report per seed -> ms_g1_check (first sample) -> structure_backtest (first sample)
#        -> structure_gate_check -> calibration_pack (env lines; nothing is applied) -> board_parity_check.
# Output: $OUT_DIR/summary.txt (everything printed) plus each tool's own files. Nothing is applied to any .env or service.
# Exit 1 when a step failed to run or G1 / the parity check says FAIL / NO_GO; a NOT_ENOUGH_DATA verdict is not a failure.
# ============================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON_BIN:-python3}"
OUT_DIR="${OUT_DIR:-$REPO_DIR/calibration_out}"
SEEDS="${SEEDS:-7 11 13}"
LIMIT="${LIMIT:-400}"
PERIOD="${PERIOD:-5y}"
STEP="${STEP:-3}"
HORIZON="${HORIZON:-10}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 1)}"
DRY_RUN="${DRY_RUN:-0}"
MARKET_DATA_URL="${MARKET_DATA_URL:-http://localhost:8001}"
API_GATEWAY_URL="${API_GATEWAY_URL:-http://localhost:8000}"
export MARKET_DATA_URL
SCRIPTS="$REPO_DIR/scripts"
FAILED=()

mkdir -p "$OUT_DIR"
SUMMARY="$OUT_DIR/summary.txt"

# run <command...> - echoes the command, runs it unless DRY_RUN=1, remembers a failure.
run() {
  printf '+ %s\n' "$*"
  [[ "$DRY_RUN" == "1" ]] && return 0
  "$@"; local rc=$?
  [[ $rc -ne 0 ]] && { printf '  (exit %s)\n' "$rc"; FAILED+=("$*"); }
  return $rc
}

main() {
  local first="" ab_args=() live_args=() src
  if [[ -n "${SYMBOLS:-}" ]]; then src=(--symbols "$SYMBOLS"); else src=(--universe-url "${API_GATEWAY_URL%/}/api/universe"); fi

  for seed in $SEEDS; do
    echo "=== seed $seed"
    local hist="$OUT_DIR/hist_$seed" ab="$OUT_DIR/ab_$seed"
    [[ -z "$first" ]] && first="$hist"
    run "$PY" "$SCRIPTS/fetch_history_for_backtest.py" "$hist" "${src[@]}" --period "$PERIOD" --sample "$LIMIT" --seed "$seed" \
        --manifest "$OUT_DIR/sample_manifest_$seed.json" --sleep 1.5 || continue
    run "$PY" "$SCRIPTS/ms_ab_report.py" "$hist" --out "$ab" --step "$STEP" --horizon "$HORIZON" --jobs "$JOBS" && ab_args+=(--ab "$ab/ab_report.json")
  done
  if [[ -z "$first" ]]; then echo "no seeds given"; FAILED+=("no seeds"); return 1; fi

  echo "=== G1 correctness (first sample)"
  run "$PY" "$SCRIPTS/ms_g1_check.py" "$first" --out "$OUT_DIR/g1"
  [[ "$DRY_RUN" != "1" && -f "$OUT_DIR/g1/g1_report.md" ]] && cat "$OUT_DIR/g1/g1_report.md"

  echo "=== walk-forward (first sample)"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ %s %s %s --step %s --horizon %s > %s\n' "$PY" "$SCRIPTS/structure_backtest.py" "$first" "$STEP" "$HORIZON" "$OUT_DIR/backtest.json"
  else
    printf '+ %s %s %s --step %s --horizon %s > %s\n' "$PY" "$SCRIPTS/structure_backtest.py" "$first" "$STEP" "$HORIZON" "$OUT_DIR/backtest.json"
    "$PY" "$SCRIPTS/structure_backtest.py" "$first" --step "$STEP" --horizon "$HORIZON" > "$OUT_DIR/backtest.json" || FAILED+=("structure_backtest")
  fi

  echo "=== gate readout (G2 / G4)"
  if [[ -n "${LIVE_REPORT_URL:-}" ]]; then
    run curl -sS -m 30 -o "$OUT_DIR/live_report.json" "$LIVE_REPORT_URL" && live_args=(--live "$OUT_DIR/live_report.json")
  fi
  if [[ ${#ab_args[@]} -gt 0 ]]; then
    run "$PY" "$SCRIPTS/structure_gate_check.py" "${ab_args[@]}" ${live_args[@]+"${live_args[@]}"}
  elif [[ "$DRY_RUN" != "1" ]]; then
    echo "no A/B report was produced, so the gate readout is skipped"
  fi

  echo "=== gate threshold suggestions (advisory)"
  if [[ "$DRY_RUN" == "1" || -s "$OUT_DIR/backtest.json" ]]; then
    run "$PY" "$SCRIPTS/calibration_pack.py" --backtest "$OUT_DIR/backtest.json" ${ab_args[@]+"${ab_args[@]}"} --out "$OUT_DIR/calibration_pack.md"
  else
    echo "no backtest output, so no suggestions"
  fi

  if [[ -n "${PS_STATUS_URL:-}" ]]; then
    echo "=== board parity (legacy removal in position-stocks)"
    local tok=(); [[ -n "${PS_TOKEN:-}" ]] && tok=(--token "$PS_TOKEN")
    run "$PY" "$SCRIPTS/board_parity_check.py" --url "$PS_STATUS_URL" ${tok[@]+"${tok[@]}"}
  else
    echo "=== board parity: skipped (set PS_STATUS_URL=http://localhost:8006/board/status)"
  fi

  echo "=== done. Summary: $SUMMARY"
  if [[ ${#FAILED[@]} -gt 0 ]]; then printf 'FAILED steps:\n'; printf '  %s\n' "${FAILED[@]}"; return 1; fi
  return 0
}

main 2>&1 | tee "$SUMMARY"
exit "${PIPESTATUS[0]}"
