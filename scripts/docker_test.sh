#!/usr/bin/env bash
# group313: run a service's tests in the SAME Python version its Dockerfile deploys (read from the FROM line), so a green test run
# means the same interpreter as production. Mounts the WHOLE repo: some tests read root files (.env.example) and tests/test_admin_auth_e2e.py
# in position-stocks-service needs ../real-trade-service, so both run instead of failing / skipping.
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
# pip reads from pypi.org with a 15 s default timeout and 5 retries; on the VM that timed out on /simple/pytest/ and the run died.
# Longer timeout + more retries, and a named volume as the pip cache so a second run (or a retry) does not download everything again.
# Override with PIP_TIMEOUT / PIP_RETRIES; PIP_INDEX_URL / PIP_EXTRA_INDEX_URL are passed through if you set them (mirror).
docker run --rm -v "$root":/repo -v stockky-pip-cache:/root/.cache/pip -w "/repo/services/$svc" \
  -e PIP_DEFAULT_TIMEOUT="${PIP_TIMEOUT:-120}" -e PIP_RETRIES="${PIP_RETRIES:-10}" \
  -e PIP_INDEX_URL -e PIP_EXTRA_INDEX_URL \
  "$tag" sh -c '
  c=""; [ -f constraints.txt ] && c="-c constraints.txt"
  pip install -q --root-user-action=ignore -r requirements.txt $c pytest pytest-cov >/dev/null && python -m pytest -q "$@"' sh "$@"
