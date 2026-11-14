#!/usr/bin/env bash
# fno_replay_vm.sh - E523 (round 503): run the F&O replay backtest on the VM in ONE command.
#   bash scripts/fno_replay_vm.sh                                   # every recorded day, NIFTY (+ SENSEX when market-data serves its candles)
#   bash scripts/fno_replay_vm.sh --from 2026-11-11 --set sl_pct=20 # extra arguments go to the replay as they are
# Why: the recorded chains live in the docker volume mounted at /data/fno INSIDE fno-options-service, not at /data/fno on the host (round 502's command found no snapshots and printed
# an empty NOT_PROVEN result). So the daily candles are exported on the host into ./data/fno_real_probe (bind-mounted into the container) and the replay runs inside the container,
# where the snapshots, the engine code and the result folder (/data/fno/backtests, which the F&O tab reads) are. Read-only on everything except that result folder.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST_DIR="${REPLAY_INPUTS:-data/fno_real_probe/replay_inputs}"     # host side of /data/fno_real_probe/replay_inputs
CT_DIR="/data/fno_real_probe/replay_inputs"
mkdir -p "$HOST_DIR"
python3 scripts/fno_export_candles.py --out "$HOST_DIR"
und="NIFTY"; extra=()
if python3 scripts/fno_export_candles.py --und SENSEX --no-vix --out "$HOST_DIR" >/dev/null 2>&1 && [ -s "$HOST_DIR/sensex_d.csv" ]; then
  und="NIFTY,SENSEX"; extra=(--daily-sensex "$CT_DIR/sensex_d.csv")
else
  echo "SENSEX daily candles not available from market-data: replaying NIFTY only"
fi
if [ -z "$(docker compose ps -q fno-options-service 2>/dev/null)" ]; then
  echo "fno-options-service is not running (docker compose ps): the recordings are inside it, start it first" >&2
  exit 1
fi
echo "== replaying inside fno-options-service: $und, snapshots /data/fno, daily $CT_DIR/nifty_d.csv"
set +e
docker compose exec -T fno-options-service python3 -c 'import sys; from backtest.cli import main_replay; sys.exit(main_replay(sys.argv[1:]))' \
  --snapshots /data/fno --daily "$CT_DIR/nifty_d.csv" ${extra[@]+"${extra[@]}"} --underlyings "$und" "$@"
rc=$?
set -e
echo "== replay finished, exit code $rc"
if [ "$rc" -ne 0 ]; then
  echo "   137 = the container killed it (memory); 126/127/1 with no text = see: docker compose logs --tail 40 fno-options-service" >&2
fi
exit "$rc"
