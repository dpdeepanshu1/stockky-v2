#!/usr/bin/env bash
# Usage (from services/analysis-intelligence-service, venv active):
#   ./run_tests.sh            # each test file in its own process + combined coverage
#   ./run_tests.sh --single   # one pytest process over tests/ (uses tests/conftest.py)
set -u
cd "$(dirname "$0")"

if [ "${1:-}" = "--single" ]; then
  python3 -m pytest tests/ -q --cov --cov-report=term-missing --cov-fail-under="${COV_MIN:-95}"
  exit $?
fi

rm -f .coverage
fail=0
for f in tests/test_*.py; do
  python3 -m pytest "$f" -q -p no:cacheprovider --cov --cov-append --cov-report= || { echo "FAILED: $f"; fail=1; }
done
# Regression gate: fail if total coverage drops below COV_MIN (default 95; override: COV_MIN=98 ./run_tests.sh)
python3 -m coverage report --fail-under="${COV_MIN:-95}" || fail=1
echo "exit=$fail"
exit $fail
