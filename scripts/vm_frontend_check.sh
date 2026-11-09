#!/usr/bin/env bash
# vm_frontend_check.sh - run the frontend tests and the TypeScript check on a host with no npm (the VM), inside a node container, WITHOUT touching frontend/ on the host.
#   cd ~/stockky-v2 && bash scripts/vm_frontend_check.sh
# Why: a plain `docker run -v frontend:/app ... npm ci` leaves a root-owned frontend/node_modules, and the next run as your user fails ("rejected by your operating system", and rm -rf fails too).
# Here the WHOLE repo is mounted READ-ONLY (several frontend tests read ../../services/**.py and other repo files, so frontend/ alone is not enough), copied inside the container without node_modules/.git/dist and thrown away with it. Exit code = test result. Add sudo before `bash` if docker needs it.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$ROOT/frontend" ] || { echo "frontend/ not found under $ROOT"; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "docker is not installed or not on PATH"; exit 2; }
docker run --rm -v "$ROOT":/src:ro node:22 sh -c '
  set -e
  mkdir -p /work && cd /src && tar --exclude=node_modules --exclude=dist --exclude=.git --exclude=./oracle_wallet --exclude=./archive -cf - . | tar -C /work -xf -
  cd /work/frontend
  npm ci --no-audit --no-fund --loglevel=error
  npm test
  npx tsc --noEmit
  echo FRONTEND_CHECK_OK
' 2>&1 | tail -${TAIL_LINES:-12}
rc=${PIPESTATUS[0]}
[ "$rc" -eq 0 ] && echo "frontend check: PASSED" || echo "frontend check: FAILED (exit $rc)"
exit "$rc"
