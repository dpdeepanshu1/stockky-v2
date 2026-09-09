#!/usr/bin/env bash
# Market-aware wake helper.
# FULL warm: Mon–Fri 08:30–16:00 IST
# LIGHT: otherwise
# Usage: export API_GATEWAY_URL=https://...; ./scripts/wake_all.sh [full|light|auto]

set -euo pipefail
GW="${API_GATEWAY_URL:-}"
if [[ -z "$GW" ]]; then
  echo "Set API_GATEWAY_URL first" >&2
  exit 1
fi
GW="${GW%/}"
MODE="${1:-auto}"

DOW=$(TZ=Asia/Kolkata date +%u)
HM=$(TZ=Asia/Kolkata date +%H%M)
FULL=0
if [[ "$DOW" -le 5 && "$HM" -ge 0830 && "$HM" -le 1600 ]]; then FULL=1; fi
[[ "$MODE" == "full" ]] && FULL=1
[[ "$MODE" == "light" ]] && FULL=0

if [[ "$FULL" -eq 1 ]]; then
  echo "[full] wake-all + keepalive + outbox + ops alert"
  curl -fsS -m 60 -X POST "$GW/wake-all" || curl -fsS -m 60 "$GW/wake-all" || true
  curl -fsS -m 25 "$GW/health?warm=true" || true
  curl -fsS -m 15 "$GW/ops/keepalive" || true
  if [[ -n "${NOTIFICATION_URL:-}" ]]; then
    N="${NOTIFICATION_URL%/}"
    curl -fsS -m 30 -X POST "$N/outbox/process" || true
  fi
  curl -fsS -m 25 -X POST "$GW/ops/check-alert" || true
else
  echo "[light] keepalive / health only"
  curl -fsS -m 20 "$GW/ops/keepalive" || curl -fsS -m 20 "$GW/health" || true
fi
echo "[wake] done"
