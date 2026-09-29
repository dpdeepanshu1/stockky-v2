#!/usr/bin/env bash
# Usage (from services/analysis-intelligence-service, venv active):
#   ./run_tests.sh            # each test file in its own process + combined coverage
#   ./run_tests.sh --single   # one pytest process over tests/ (uses tests/conftest.py)
set -u
cd "$(dirname "$0")"

if [ "${1:-}" = "--single" ]; then
  python3 -m pytest tests/ -q --cov --cov-report=term-missing
  exit $?
fi

rm -f .coverage
fail=0
for f in tests/test_*.py; do
  python3 -m pytest "$f" -q -p no:cacheprovider --cov --cov-append --cov-report= || { echo "FAILED: $f"; fail=1; }
done
python3 -m coverage report
echo "exit=$fail"
exit $fail
