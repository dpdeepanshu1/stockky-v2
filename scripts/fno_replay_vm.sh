#!/usr/bin/env bash
# fno_replay_vm.sh - E523 / E525 (rounds 503, 505): run the F&O replay backtest on the VM in ONE command, WITHOUT touching the live service.
#   bash scripts/fno_replay_vm.sh                                   # every recorded day, NIFTY (+ SENSEX when market-data serves its candles)
#   bash scripts/fno_replay_vm.sh --from 2026-11-11 --set sl_pct=20 # extra arguments go to the replay as they are
#   REPLAY_MEM=5g bash scripts/fno_replay_vm.sh                     # memory cap of the replay container (default 3g)
# The recorded chains live in the docker volume mounted at /data/fno INSIDE fno-options-service, not on the host. Round 503 ran the replay with `docker compose exec` in the LIVE container:
# it was killed (exit 137) and the container restarted, which would have stopped the engine during a trading day. Now the replay runs in its OWN throw-away container made from the same
# image, with the recordings mounted READ-ONLY, the code of this checkout mounted read-only (no image rebuild needed), no network, and a hard memory cap: if it ever runs out of memory only
# that container dies. The result files are written on the host in ./data/fno_real_probe/replay_out (the report text is printed too).
set -euo pipefail
cd "$(dirname "$0")/.."
HOST_DIR="${REPLAY_INPUTS:-data/fno_real_probe/replay_inputs}"     # host side of /data/fno_real_probe/replay_inputs
OUT_DIR="${REPLAY_OUT:-data/fno_real_probe/replay_out}"            # host side of /data/fno_real_probe/replay_out
CT_IN="/data/fno_real_probe/replay_inputs"
CT_OUT="/data/fno_real_probe/replay_out"
MEM="${REPLAY_MEM:-3g}"
mkdir -p "$HOST_DIR" "$OUT_DIR"
python3 scripts/fno_export_candles.py --out "$HOST_DIR"
und="NIFTY"; extra=()
if python3 scripts/fno_export_candles.py --und SENSEX --no-vix --out "$HOST_DIR" >/dev/null 2>&1 && [ -s "$HOST_DIR/sensex_d.csv" ]; then
  und="NIFTY,SENSEX"; extra=(--daily-sensex "$CT_IN/sensex_d.csv")
else
  echo "SENSEX daily candles not available from market-data: replaying NIFTY only"
fi
cid="$(docker compose ps -q fno-options-service 2>/dev/null || true)"
if [ -z "$cid" ]; then
  echo "fno-options-service is not running (docker compose ps): its image and recordings are needed, start it first" >&2
  exit 1
fi
image="$(docker inspect -f '{{.Config.Image}}' "$cid")"
vol="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data/fno"}}{{.Source}}{{end}}{{end}}' "$cid")"
if [ -z "$image" ] || [ -z "$vol" ]; then
  echo "cannot read the image or the /data/fno mount of fno-options-service (docker inspect $cid)" >&2
  exit 1
fi
echo "== recordings (size per day):"
docker compose exec -T fno-options-service sh -c 'du -sh /data/fno/snapshots/* 2>/dev/null | tail -20' || true
echo "== replaying in a separate container ($image, memory cap $MEM): $und"
set +e
docker run --rm --memory "$MEM" --memory-swap "$MEM" --network none \
  -v "$vol":/data/fno:ro \
  -v "$PWD/services/fno-options-service":/app:ro \
  -v "$PWD/data/fno_real_probe":/data/fno_real_probe \
  "$image" python3 -c 'import sys; from backtest.cli import main_replay; sys.exit(main_replay(sys.argv[1:]))' \
  --snapshots /data/fno --daily "$CT_IN/nifty_d.csv" ${extra[@]+"${extra[@]}"} --underlyings "$und" --out "$CT_OUT" "$@"
rc=$?
set -e
echo "== replay finished, exit code $rc (result files: $OUT_DIR/backtests/)"
if [ "$rc" -eq 137 ]; then
  echo "   137 = the replay container ran out of its memory cap ($MEM); the live service is untouched. Raise it: REPLAY_MEM=6g bash scripts/fno_replay_vm.sh" >&2
elif [ "$rc" -ne 0 ]; then
  echo "   non-zero exit: read the lines above; the live service is untouched" >&2
fi
exit "$rc"
