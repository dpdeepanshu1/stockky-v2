#!/usr/bin/env bash
# group313: run a service's tests in the SAME Python version its Dockerfile deploys (read from the FROM line), so a green test run
# means the same interpreter as production. Mounts ./services so tests that reach a sibling service (tests/test_admin_auth_e2e.py
# in position-stocks-service needs ../real-trade-service) run instead of skipping.
#
#   scripts/docker_test.sh position-stocks-service                 # whole suite
#   scripts/docker_test.sh real-trade-service tests/test_x.py -x   # extra args go to pytest
# Uses constraints.txt when present (scripts/freeze_constraints.sh), so the tests run on the pinned versions.
set -euo pipefail
svc="${1:?usage: scripts/docker_test.sh <service-dir-name> [pytest args]}"; shift || true
root="$(cd "$(dirname "$0")/.." && pwd)"
tag="$(grep -m1 -E '^FROM python:' "$root/services/$svc/Dockerfile" | sed -E 's/^FROM (python:[^ ]+).*/\1/')"
[ -n "$tag" ] || { echo "no 'FROM python:' line in services/$svc/Dockerfile"; exit 2; }
echo ">> $svc on $tag (same as its Dockerfile)"
docker run --rm -v "$root/services":/services -w "/services/$svc" "$tag" sh -c '
  c=""; [ -f constraints.txt ] && c="-c constraints.txt"
  pip install -q --root-user-action=ignore -r requirements.txt $c pytest pytest-cov >/dev/null && python -m pytest -q "$@"' sh "$@"
