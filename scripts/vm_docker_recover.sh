#!/usr/bin/env bash
# vm_docker_recover.sh - get the stack back up after "failed to prepare extraction snapshot ... parent snapshot ... does not exist: not found" (group 427).
#
# What that error is: Docker's image store (containerd) lost a layer that one image still points at - usually after an interrupted build, a prune that ran
# during a build, or a full disk. It is NOT a code problem, and `docker compose down` + `up --build` leaves the whole stack stopped while it fails.
# What this does, stopping at the first step that works (it never touches volumes, so databases / .env data stay):
#   1. plain rebuild of the broken service(s)
#   2. remove that service's image + clear the BUILD cache, rebuild with --no-cache
#   3. restart docker + containerd (needs sudo), rebuild again
#   4. bring everything up
# Usage:  bash scripts/vm_docker_recover.sh [service ...]      (default: analysis-intelligence-service)
set -u
cd "$(dirname "$0")/.." || exit 2
SERVICES=("$@")
[ ${#SERVICES[@]} -eq 0 ] && SERVICES=(analysis-intelligence-service)

say() { printf '\n==> %s\n' "$*"; }
build() { docker compose build "$@" ; }

say "disk space (a full disk is the usual cause; need several GB free)"
df -h / /var/lib/docker /var/lib/containerd 2>/dev/null | awk '!seen[$0]++'

say "step 1: plain rebuild of: ${SERVICES[*]}"
if build "${SERVICES[@]}"; then OK=1; else OK=0; fi

if [ "$OK" -eq 0 ]; then
  say "step 2: drop the broken image(s) and the build cache, rebuild without cache"
  for s in "${SERVICES[@]}"; do
    img="$(docker compose config --images 2>/dev/null | grep -E "(^|/)stockky-v2-${s}(:|$)" | head -1)"
    [ -z "$img" ] && img="stockky-v2-${s}:latest"
    docker image rm -f "$img" 2>/dev/null || true
  done
  docker builder prune -af
  if build --no-cache "${SERVICES[@]}"; then OK=1; fi
fi

if [ "$OK" -eq 0 ]; then
  say "step 3: restart docker + containerd, then rebuild without cache"
  sudo systemctl restart containerd docker && sleep 5
  if build --no-cache "${SERVICES[@]}"; then OK=1; fi
fi

if [ "$OK" -eq 0 ]; then
  say "STILL FAILING. Last resort (removes ALL unused images and build cache, NOT volumes): docker system prune -af ; then run this script again."
  say "If the disk is nearly full, free space first (df -h)."
  exit 1
fi

say "step 4: start the stack"
docker compose up -d --build || { echo "compose up failed - see the message above"; exit 1; }
docker compose ps
say "done. Follow the logs with: docker compose logs -f --tail=100"
