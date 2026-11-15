#!/usr/bin/env bash
# E565 (round 540): Agent Round 3 helper. Run it AFTER the runner page's "run agent now" (Mon 16 Nov, after 15:45 IST).
#   bash scripts/agent_round3.sh                      # print today's agent file (IST date) and the current AGENT_MODE
#   bash scripts/agent_round3.sh 2026-11-16           # a given day
#   bash scripts/agent_round3.sh --apply-propose      # after you read it: AGENT_MODE=propose in .env, then restart the runner
# It never starts the agent (that needs the admin login on the runner page) and never changes anything without --apply-propose.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT" || exit 2
APPLY=0; DAY=""
for a in "$@"; do case "$a" in --apply-propose) APPLY=1;; [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) DAY="$a";; *) echo "unknown argument: $a"; exit 2;; esac; done
DAY="${DAY:-$(TZ=Asia/Kolkata date +%F)}"
SVC=calibration-runner-service
F=$(docker compose exec -T $SVC sh -c "find / -path '*agent/$DAY.json' 2>/dev/null | head -1" 2>/dev/null | tr -d '\r')
MODE=$(docker compose exec -T $SVC printenv AGENT_MODE 2>/dev/null | tr -d '\r')
echo "day: $DAY | AGENT_MODE in the container: ${MODE:-unknown} | .env: $(grep -E '^AGENT_MODE=' .env 2>/dev/null || echo '(not set, default report)')"
if [ -z "$F" ]; then
  echo "NOT WRITTEN YET for $DAY: open the runner page -> run agent now (weekdays after 15:45 IST), read Telegram, then run this again."
  [ "$APPLY" -eq 1 ] && echo "refusing --apply-propose: there is no agent file to review."
  exit 1
fi
echo "file: $F"; echo "-----"
docker compose exec -T $SVC sh -c "head -c 3000 '$F'"; echo; echo "-----"
if [ "$APPLY" -ne 1 ]; then
  echo "If this looks right: bash scripts/agent_round3.sh --apply-propose"; exit 0
fi
[ "$MODE" = "propose" ] && { echo "AGENT_MODE is already propose, nothing to do."; exit 0; }
[ -f .env ] || { echo ".env not found in $ROOT"; exit 1; }
cp .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
python3 - <<'PY'
import re
p=".env"; t=open(p,encoding="utf8").read()
if re.search(r"(?m)^AGENT_MODE=", t): t=re.sub(r"(?m)^AGENT_MODE=.*$", "AGENT_MODE=propose", t)
else: t=t.rstrip("\n")+"\nAGENT_MODE=propose\n"
open(p,"w",encoding="utf8").write(t)
PY
grep -E '^AGENT_MODE=' .env
docker compose up -d $SVC
sleep 6
echo "AGENT_MODE in the container now: $(docker compose exec -T $SVC printenv AGENT_MODE 2>/dev/null | tr -d '\r')"
docker compose ps $SVC
