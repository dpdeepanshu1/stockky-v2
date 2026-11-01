#!/usr/bin/env bash
# E284: the E278 sweep study in ONE command, on the VM HOST (run from the repo folder, next to docker-compose.yml). Needs python3 and docker; nothing to edit.
#   bash scripts/e278_run.sh                 # all names in SWING_UNIVERSE_SYMBOLS (.env), 5 years of daily history, then the study
#   bash scripts/e278_run.sh --skip-fetch    # reuse the history already in $HIST (a re-run takes seconds)
#   bash scripts/e278_run.sh --sample 200    # a random 200 of the universe (faster, still unbiased)
# Folders (override by exporting before the command): HIST=~/hist744  BARS=~/bar_copy  OUT=~/sweep_out.  Market data: $MARKET_DATA_URL, else http://127.0.0.1:8001
# (the port docker-compose.yml publishes on the host). Offline study: it reads files and one read-only history route, never touches an order, a switch or the database.
cd "$(dirname "$0")/.." || exit 2
HIST="${HIST:-$HOME/hist744}"; BARS="${BARS:-$HOME/bar_copy}"; OUT="${OUT:-$HOME/sweep_out}"
BASE="${MARKET_DATA_URL:-http://127.0.0.1:8001}"
SKIP_FETCH=0; SAMPLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-fetch) SKIP_FETCH=1 ;;
    --sample) shift; SAMPLE="${1:-0}" ;;
    *) echo "unknown option: $1 (use --skip-fetch or --sample N)"; exit 2 ;;
  esac
  shift
done
step(){ echo; echo "== $*"; }
die(){ echo "STOP: $*" >&2; exit 1; }

step "1. the universe (SWING_UNIVERSE_SYMBOLS in .env)"
[ -f .env ] || die ".env not found here; run this from the repo folder"
SYMS=/tmp/e278_symbols.txt
grep -m1 '^[[:space:]]*SWING_UNIVERSE_SYMBOLS=' .env | cut -d= -f2- | tr -d '"'"'"'\r' | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | sed '/^$/d' > "$SYMS"
N=$(wc -l < "$SYMS" | tr -d ' ')
[ "${N:-0}" -ge 1 ] || die "SWING_UNIVERSE_SYMBOLS is empty or missing in .env"
echo "$N symbols"

if [ "$SKIP_FETCH" -eq 0 ]; then
  step "2. daily history from $BASE (resumes: files already in $HIST are kept)"
  python3 - "$BASE" <<'PY' || die "market-data is not answering at $BASE. Check: docker compose ps market-data-service  (or export MARKET_DATA_URL=http://host:port)"
import sys, urllib.request
urllib.request.urlopen(sys.argv[1].rstrip("/") + "/health", timeout=15).read()
PY
  EXTRA=()
  [ "$SAMPLE" -gt 0 ] && EXTRA=(--sample "$SAMPLE")
  MARKET_DATA_URL="$BASE" python3 scripts/fetch_history_for_backtest.py "$HIST" --symbols-file "$SYMS" --period 5y --limit 0 --min-bars 250 "${EXTRA[@]}" \
    || echo "NOTE: the fetch reported failures; the study still runs on the files that were saved"
else
  step "2. history: skipped (using $HIST)"
fi
[ -d "$HIST" ] && [ "$(ls "$HIST" | wc -l)" -ge 1 ] || die "no daily history in $HIST (part A cannot run)"

step "3. copy the recorded 1-minute bars to $BARS"
V=$(docker volume ls -q | grep -E 'bar-records$' | head -1)
[ -n "$V" ] || die "no docker volume named *bar-records (docker volume ls)"
mkdir -p "$BARS"
docker run --rm -v "$V":/d:ro -v "$BARS":/out alpine sh -c "cp -a /d/. /out/ && chown -R $(id -u):$(id -g) /out" || die "copying the volume $V failed"
echo "volume $V -> $BARS: $(find "$BARS" -maxdepth 1 -type d -name '20*' | wc -l | tr -d ' ') day folder(s), $(find "$BARS" -name '*.json' -path '*/20*' | wc -l | tr -d ' ') file(s)"

step "4. the study (it prints what it read from each folder, and why a part is empty)"
mkdir -p "$OUT"
python3 scripts/sweep_study.py --daily "$HIST" --bars "$BARS" --out "$OUT" | tee /tmp/sweep.md
echo; echo "saved: $OUT/sweep_study.md and $OUT/sweep_study.json   (paste /tmp/sweep.md back)"
