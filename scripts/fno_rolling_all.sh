#!/usr/bin/env bash
# fno_rolling_all.sh - round 663 (F3): the rolling (REAL-premium) backtests the F&O readiness list asks for, in ONE command, for the 6 strategies REAL would trade
# (CORE TREND_RIDER MOMENTUM_SURGE BEAR_SWEEP DRIFT_EDGE WEEKLY_OPENER) x NIFTY and SENSEX = 12 results, saved where fno/readiness.py looks (/data/fno/backtests in fno-options-service).
#
#   export DHAN_CLIENT_ID=...  DHAN_ACCESS_TOKEN=...          # the Dhan DATA API plan is needed (Rs ~499 a month, see backtest/rolling.py); read-only calls, no orders
#   bash scripts/fno_rolling_all.sh                            # all stages, 180 days back
#   FROM=2026-06-01 bash scripts/fno_rolling_all.sh            # a different start (TO defaults to today; the last day is not included)
#   STAGES="build run publish" bash scripts/fno_rolling_all.sh # skip the fetch (data already in $ROLL_DIR)
#   DRY_RUN=1 bash scripts/fno_rolling_all.sh                  # print every command, run nothing
#
# Stages (default "fetch check build run publish"):
#   fetch    scripts/fno_rolling_fetch.py for NIFTY and SENSEX, strikes ATM, ATM+-1, ATM+-2, with --resume (a rerun only asks for what is missing or failed)
#   check    scripts/fno_rolling_check.py on $ROLL_DIR - stops here when a file has a PROBLEM (a backtest on a broken series says nothing)
#   build    daily index candles + VIX (market-data on 127.0.0.1:8001) and the 5-minute index candles derived from the rolling files -> $INPUTS
#   run      12 backtests -> $OUT/backtests/fno_rolling_*.json / .txt ; a one-line summary per result is printed at the end
#   publish  docker cp of those files into the running fno-options-service volume (/data/fno/backtests). Copy only - nothing is restarted.
#
# RULES: (1) NOT in market hours (Mon-Fri 09:00-15:30 IST) unless FORCE=1 - the backtests are heavy and the fetch shares the Dhan rate limit with live trading (plan rule I2).
#        (2) SENSEX uses underlying id 51, which backtest/rolling.py marks as NOT verified by a live call: the fetch stage for SENSEX tells you if Dhan refuses it; NIFTY still runs.
#        (3) A result only counts for readiness when data_quality is ok, it has at least 30 trades, net is positive after charges, and it is younger than 14 days.
#        (4) The rolling files give one spot per candle, not a real high / low, so range- and breakout-based strategies see fewer breakouts than live: a result is a floor for those.
set -uo pipefail
cd "$(dirname "$0")/.."

ROLL_DIR="${ROLL_DIR:-data/fno_rolling_long}"
INPUTS="${INPUTS:-data/fno_real_probe/replay_inputs}"
OUT="${OUT:-data/fno_rolling_out}"
STAGES="${STAGES:-fetch check build run publish}"
STRATEGIES="${STRATEGIES:-CORE TREND_RIDER MOMENTUM_SURGE BEAR_SWEEP DRIFT_EDGE WEEKLY_OPENER}"
UNDERLYINGS="${UNDERLYINGS:-NIFTY SENSEX}"
STRIKES="${STRIKES:-ATM,ATM+1,ATM-1,ATM+2,ATM-2}"
FROM="${FROM:-$(date -d '180 days ago' +%F)}"
TO="${TO:-$(date +%F)}"
DRY="${DRY_RUN:-0}"

run() { echo "+ $*"; if [ "$DRY" = "1" ]; then return 0; fi; "$@"; }
has() { case " $STAGES " in *" $1 "*) return 0;; esac; return 1; }

# rule 1: market hours (IST), weekdays
dow="$(TZ=Asia/Kolkata date +%u)"; hm="$(TZ=Asia/Kolkata date +%H%M)"
if [ "${FORCE:-0}" != "1" ] && [ "$dow" -le 5 ] && [ "$hm" -ge 0900 ] && [ "$hm" -lt 1530 ]; then
  echo "refusing to run in market hours (IST $hm, weekday). Run after 15:30, or FORCE=1 if you really mean it." >&2
  exit 3
fi

if has fetch && [ "$DRY" != "1" ] && { [ -z "${DHAN_CLIENT_ID:-}" ] || [ -z "${DHAN_ACCESS_TOKEN:-}" ]; }; then
  echo "fetch stage needs DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in the environment (Dhan Data API plan). Or skip it: STAGES=\"build run publish\"" >&2
  exit 2
fi
mkdir -p "$ROLL_DIR" "$INPUTS" "$OUT"
echo "== range $FROM .. $TO (last day not included)  strategies: $STRATEGIES  underlyings: $UNDERLYINGS"

fetch_failed=""
if has fetch; then
  for u in $UNDERLYINGS; do
    echo "== fetch $u"
    if ! run python3 scripts/fno_rolling_fetch.py --und "$u" --from "$FROM" --to "$TO" --strikes "$STRIKES" --out "$ROLL_DIR" --resume; then
      echo "!! fetch for $u did not finish cleanly (see above). A series with failed windows is saved as .partial and is NOT used." >&2
      fetch_failed="$fetch_failed $u"
    fi
  done
fi

if has check; then
  echo "== check $ROLL_DIR"
  if ! run python3 scripts/fno_rolling_check.py --dir "$ROLL_DIR"; then
    echo "!! the check found a PROBLEM in the rolling files. Fix or refetch before trusting any backtest. Stopping." >&2
    exit 4
  fi
fi

usable=""
if has build; then
  for u in $UNDERLYINGS; do
    l="$(echo "$u" | tr 'A-Z' 'a-z')"
    echo "== build inputs $u"
    if run python3 scripts/fno_export_candles.py --und "$u" --period 1y --out "$INPUTS" \
       && run python3 scripts/fno_rolling_index_candles.py --rolling-dir "$ROLL_DIR" --und "$u" --out "$INPUTS/${l}_5m.csv"; then
      usable="$usable $u"
    else
      echo "!! inputs for $u could not be built (no rolling files for it, or market-data not answering on 127.0.0.1:8001): $u is skipped" >&2
    fi
  done
else
  usable="$UNDERLYINGS"
fi

summary=()
if has run; then
  for u in $usable; do
    l="$(echo "$u" | tr 'A-Z' 'a-z')"
    vix=(); [ -s "$INPUTS/vix.csv" ] && vix=(--vix "$INPUTS/vix.csv")
    for s in $STRATEGIES; do
      echo "== backtest $u $s"
      cmd=(python3 scripts/fno_rolling_backtest.py --underlying "$u" --strategy "$s" --daily "$INPUTS/${l}_d.csv" --intraday "$INPUTS/${l}_5m.csv"
           --rolling-dir "$ROLL_DIR" ${vix[@]+"${vix[@]}"} --out "$OUT")
      echo "+ ${cmd[*]}"
      if [ "$DRY" = "1" ]; then summary+=("dry  $u $s"); continue; fi
      if "${cmd[@]}" > "$OUT/last_${l}_${s}.log" 2>&1; then
        summary+=("ok   $u $s")
      else
        summary+=("FAIL $u $s  (see $OUT/last_${l}_${s}.log: $(tail -1 "$OUT/last_${l}_${s}.log" 2>/dev/null))")
      fi
    done
  done
  echo; echo "== summary"
  printf '%s\n' ${summary[@]+"${summary[@]}"}
  if [ "$DRY" != "1" ]; then
    python3 - "$OUT/backtests" <<'PY'
import glob, json, os, sys
best = {}
for f in glob.glob(os.path.join(sys.argv[1], "fno_rolling*.json")):
    try:
        r = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    k = (str(r.get("strategy") or "CORE").upper(), str(r.get("underlying") or "").upper())
    if k not in best or os.path.getmtime(f) > best[k][0]:
        best[k] = (os.path.getmtime(f), r, os.path.basename(f))
print(f"{'strategy':<16}{'und':<8}{'trades':>7}{'net':>12}  {'data ok':<8}verdict  (counts for readiness: >=30 trades, net>0, data ok, PASSED)")
for (s, u), (_, r, name) in sorted(best.items()):
    o = (r.get("metrics") or {}).get("overall") or {}
    dq = (r.get("data_quality") or {}).get("ok")
    v = (r.get("verdict") or {}).get("status")
    n = o.get("trades") or 0
    ok = bool(dq) and n >= 30 and (o.get("net") or 0) > 0 and v == "PASSED_THIS_TEST_ONLY"
    print(f"{s:<16}{u:<8}{n:>7}{(o.get('net') if o.get('net') is not None else float('nan')):>12.0f}  {str(dq):<8}{v}  {'<- counts' if ok else ''}")
PY
  fi
fi

if has publish; then
  cid="$(docker compose ps -q fno-options-service 2>/dev/null || true)"
  if [ -z "$cid" ]; then
    echo "!! fno-options-service is not running: nothing published. The files are in $OUT/backtests; rerun with STAGES=publish later." >&2
  elif [ "$DRY" = "1" ]; then
    echo "+ docker cp $OUT/backtests/. $cid:/data/fno/backtests/   (only fno_rolling_* files)"
  else
    n=0
    for f in "$OUT"/backtests/fno_rolling_*; do
      [ -e "$f" ] || continue
      docker cp "$f" "$cid:/data/fno/backtests/$(basename "$f")" >/dev/null && n=$((n+1))
    done
    echo "== published $n files to fno-options-service:/data/fno/backtests (readiness reads them on its next refresh)"
  fi
fi
[ -n "$fetch_failed" ] && echo "NOTE: the fetch had trouble for:$fetch_failed" >&2
exit 0
