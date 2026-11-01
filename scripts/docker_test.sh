#!/usr/bin/env bash
# group313: run a service's tests in the SAME Python version its Dockerfile deploys (read from the FROM line), so a green test run
# means the same interpreter as production. Mounts the WHOLE repo: some tests read root files (.env.example) and tests/test_admin_auth_e2e.py
# in position-stocks-service needs ../real-trade-service, so both run instead of failing / skipping.
#
#   scripts/docker_test.sh position-stocks-service                 # whole suite
#   scripts/docker_test.sh real-trade-service tests/test_x.py -x   # extra args go to pytest
# Uses constraints.txt when present (scripts/freeze_constraints.sh), so the tests run on the pinned versions.
#
# E285 round 124: a service that lists torch (kronos-forecast-service) gets the CPU wheel FIRST, exactly as its Dockerfile does. `pip install -r requirements.txt` alone
# pulled the default GPU build from PyPI (several GB) and the VM disk filled up ("No space left on device"). The kronos tests do not import torch at all.
# Before anything is pulled, the free space of the disk that holds Docker's data is checked: DOCKER_TEST_MIN_FREE_GB (default 3; 0 = skip the check).
# TORCH_INDEX_URL overrides the CPU wheel index (default https://download.pytorch.org/whl/cpu).
set -euo pipefail
svc="${1:?usage: scripts/docker_test.sh <service-dir-name> [pytest args]}"; shift || true
root="$(cd "$(dirname "$0")/.." && pwd)"
tag="$(grep -m1 -E '^FROM python:' "$root/services/$svc/Dockerfile" | sed -E 's/^FROM (python:[^ ]+).*/\1/')"
[ -n "$tag" ] || { echo "no 'FROM python:' line in services/$svc/Dockerfile"; exit 2; }
echo ">> $svc on $tag (same as its Dockerfile)"
need_gb="${DOCKER_TEST_MIN_FREE_GB:-3}"
if [ "${need_gb:-0}" -gt 0 ] 2>/dev/null; then
  dr="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  free_kb=""; where="/"
  for d in "$dr" /; do
    [ -n "$d" ] || continue
    free_kb="$(df -Pk "$d" 2>/dev/null | awk 'NR==2{print $4}')"
    if [ -n "$free_kb" ]; then where="$d"; break; fi
  done
  if [ -n "$free_kb" ] && [ "$free_kb" -lt $((need_gb * 1024 * 1024)) ] 2>/dev/null; then
    echo "NOT STARTED - only $((free_kb / 1024)) MB free on $where; the test image needs about ${need_gb} GB. Free some space first:" >&2
    echo "  docker builder prune -f && docker image prune -f      (do NOT add --volumes: it would delete runner-data and bar-records)" >&2
    echo "  df -h / ; docker system df                             (see what is using the disk)" >&2
    echo "  DOCKER_TEST_MIN_FREE_GB=0 scripts/docker_test.sh ...   (skip this check)" >&2
    exit 3
  fi
fi
# pip reads from pypi.org with a 15 s default timeout and 5 retries; on the VM that timed out on /simple/pytest/ and the run died.
# Longer timeout + more retries, and a named volume as the pip cache so a second run (or a retry) does not download everything again.
# Override with PIP_TIMEOUT / PIP_RETRIES; PIP_INDEX_URL / PIP_EXTRA_INDEX_URL are passed through if you set them (mirror).
docker run --rm -v "$root":/repo -v stockky-pip-cache:/root/.cache/pip -w "/repo/services/$svc" \
  -e PIP_DEFAULT_TIMEOUT="${PIP_TIMEOUT:-120}" -e PIP_RETRIES="${PIP_RETRIES:-10}" \
  -e PIP_INDEX_URL -e PIP_EXTRA_INDEX_URL -e TORCH_INDEX_URL \
  "$tag" sh -c '
  c=""; [ -f constraints.txt ] && c="-c constraints.txt"
  # E127: fastapi.testclient needs httpx; calibration-runner-service does not list it and died at collection ("No module named httpx").
  # httpx < 0.28 because that service pins fastapi 0.104 (starlette 0.27), whose TestClient passes app= (removed in httpx 0.28).
  h=""; grep -qi "^httpx" requirements.txt || h="httpx<0.28"
  # E285 round 124: CPU torch first (the Dockerfile does the same); `torch>=2.0.0` in requirements.txt is then already satisfied, so the GPU wheel is never pulled.
  if grep -qiE "^torch([<>=!~ ]|$)" requirements.txt; then
    echo ">> this service lists torch: installing the CPU wheel first (not the GPU build)"
    pip install -q --root-user-action=ignore torch --index-url "${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}" >/dev/null \
      || { echo "pip could not install CPU torch (a full disk shows as: No space left on device; see df -h / and docker system df)" >&2; exit 4; }
  fi
  pip install -q --root-user-action=ignore -r requirements.txt $c pytest pytest-cov $h >/dev/null \
    || { echo "pip install failed (a full disk shows as: No space left on device; see df -h / and docker system df)" >&2; exit 4; }
  python -m pytest -q "$@"' sh "$@"
