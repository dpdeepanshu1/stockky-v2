#!/usr/bin/env bash
# Usage (from services/api-gateway, venv active; use `bash run_tests.sh` if not executable):
#   bash run_tests.sh            # each test file in its own process + combined coverage
#   bash run_tests.sh --single   # one pytest process over tests/
# Coverage gate: fails below COV_MIN (default 0 while the api-gateway suite is being built up;
# raise it as passes land, e.g. COV_MIN=10 bash run_tests.sh --single).
set -u
cd "$(dirname "$0")"
MIN="${COV_MIN:-0}"

if [ "${1:-}" = "--single" ]; then
  python3 -m pytest tests/ -q --cov --cov-report=term-missing --cov-fail-under="$MIN"
  exit $?
fi

rm -f .coverage
fail=0
for f in tests/test_*.py; do
  python3 -m pytest "$f" -q -p no:cacheprovider --cov --cov-append --cov-report= || { echo "FAILED: $f"; fail=1; }
done
python3 -m coverage report --fail-under="$MIN" || fail=1
echo "exit=$fail"
exit $fail
