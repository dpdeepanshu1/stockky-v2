#!/usr/bin/env bash
# E516 (round 496): the whole VM check in one command, with a summary that can be trusted.
#   cd ~/stockky-v2 && bash scripts/vm_full_check.sh                 # repo checks + every service suite in Docker + the frontend
#   SERVICES="fno-options-service real-trade-service" bash scripts/vm_full_check.sh     # only these service suites
#   DOCKER_BIN=<name> is only for tests (the program looked up to decide whether Docker exists)
#   SKIP_FRONTEND=1 bash scripts/vm_full_check.sh                    # no frontend step (no Node image on this host)
#   OUT=~/full_check bash scripts/vm_full_check.sh                   # where the per-step logs go (default: ~/stockky_full_check_<time>)
#
# Why this file exists (review items 9 and 10 of 13 Nov):
#  9. an earlier unshipped copy of this helper mounted only frontend/ into the Node container, so calibration.test.mjs, swing.test.mjs, reconcile.test.mjs,
#     statusProbes.test.mjs and liveStateView.test.mjs died with ENOENT on /services/... (they read services/**.py through new URL("../../", import.meta.url)).
#     The frontend step here calls scripts/vm_frontend_check.sh, which mounts the WHOLE repo read-only, and the step is refused up front when a file those tests read is missing.
# 10. the old summary printed exit=0 for a failed frontend test because the exit code was read after `| tail` (the code of tail, not of the test). Here every step writes its
#     full output to its own log file with no pipe in between, the code is read on the next line, and the summary is built from those codes only.
#     A step that did not run (no Docker, no Node, no tests found) is SKIP, never PASS. The script exits 1 when any step FAILED and 0 only when none did.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2
OUT="${OUT:-$HOME/stockky_full_check_$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT" || { echo "cannot create $OUT"; exit 2; }
PYTHON="${PYTHON:-python3}"

NAMES=(); CODES=(); LOGS=(); VERDICTS=()

# step <name> <command...>: run the command, output -> $OUT/<n>-<name>.log (nothing pipes it), record the REAL exit code, show the tail only when it failed.
step() {
  local name="$1"; shift
  local n=$(( ${#NAMES[@]} + 1 )) log rc
  log="$OUT/$(printf '%02d' "$n")-${name//[^A-Za-z0-9._-]/_}.log"
  echo ">> [$n] $name"
  "$@" > "$log" 2>&1
  rc=$?
  NAMES+=("$name"); CODES+=("$rc"); LOGS+=("$log")
  if [ "$rc" -eq 0 ]; then VERDICTS+=("PASS"); else VERDICTS+=("FAIL"); echo "   FAILED (exit $rc), last lines of $log:"; tail -n "${TAIL_LINES:-15}" "$log" | sed 's/^/   | /'; fi
}

# skipped <name> <why>: a step that could not run is its own verdict, so the summary never counts it as a pass.
skipped() {
  NAMES+=("$1"); CODES+=("-"); LOGS+=("-"); VERDICTS+=("SKIP")
  echo ">> [$(( ${#NAMES[@]} ))] $1 - SKIPPED: $2"
}

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------
# 1. repo checks (no Docker)
step "merge-markers"     "$PYTHON" scripts/check_merge_conflicts.py
step "shared-copies"     "$PYTHON" scripts/check_shared_copies.py
step "env-reference"     "$PYTHON" scripts/gen_env_reference.py --check
step "control-catalog"   "$PYTHON" scripts/check_control_catalog.py
step "file-manifest"     "$PYTHON" scripts/check_stale_files.py --verify
step "repo-script-tests" "$PYTHON" -m pytest scripts/test_repo_scripts.py scripts/test_docker_helper_scripts.py scripts/test_vm_full_check.py -q

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------
# 2. one Docker suite per service (the same Python as its Dockerfile, scripts/docker_test.sh)
if command -v "${DOCKER_BIN:-docker}" >/dev/null 2>&1; then
  if [ -n "${SERVICES:-}" ]; then svcs="$SERVICES"; else svcs="$(cd services && for d in */; do d="${d%/}"; [ -d "$d/tests" ] && [ -f "$d/Dockerfile" ] && echo "$d"; done)"; fi
  for s in $svcs; do
    step "tests:$s" bash scripts/docker_test.sh "$s" -q
  done
else
  skipped "service-suites" "docker is not on PATH"
fi

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------
# 3. frontend (item 9: the whole repo is mounted, not just frontend/)
if [ "${SKIP_FRONTEND:-0}" = "1" ]; then
  skipped "frontend" "SKIP_FRONTEND=1"
elif ! command -v "${DOCKER_BIN:-docker}" >/dev/null 2>&1; then
  skipped "frontend" "docker is not on PATH"
else
  # every services/... or frontend/... file a frontend test reads must exist in this checkout, or the container could not see it either
  missing="$("$PYTHON" - <<'PY'
import re, pathlib
root = pathlib.Path(".")
src = root / "frontend" / "src"
need = set()
for t in src.rglob("*.test.mjs"):
    text = t.read_text(encoding="utf-8")
    need.update(re.findall(r'new URL\("(services/[^"]+)", root\)', text))
    need.update(m for m in re.findall(r'new URL\("\.\./\.\./((?:services|frontend)/[^"]+)", import\.meta\.url\)', text))
print("\n".join(sorted(p for p in need if not (root / p).exists())))
PY
)"
  if [ -n "$missing" ]; then
    NAMES+=("frontend"); CODES+=("2"); LOGS+=("-"); VERDICTS+=("FAIL")
    echo ">> [$(( ${#NAMES[@]} ))] frontend - FAILED before starting: frontend tests read files this checkout does not have:"; echo "$missing" | sed 's/^/   | /'
  else
    TAIL_LINES=400 step "frontend" bash scripts/vm_frontend_check.sh
  fi
fi

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------
# summary: built only from the recorded codes
echo; echo "=================== SUMMARY (logs in $OUT)"
pass=0; fail=0; skip=0
for i in "${!NAMES[@]}"; do
  printf '  %-4s %-34s exit=%-3s %s\n' "${VERDICTS[$i]}" "${NAMES[$i]}" "${CODES[$i]}" "${LOGS[$i]}"
  case "${VERDICTS[$i]}" in PASS) pass=$((pass+1));; FAIL) fail=$((fail+1));; *) skip=$((skip+1));; esac
done
echo "  passed=$pass failed=$fail skipped=$skip"
if [ "$fail" -gt 0 ]; then echo "RESULT: FAILED ($fail step(s)); read the log of each FAIL line above"; exit 1; fi
if [ "$pass" -eq 0 ]; then echo "RESULT: NOTHING RAN"; exit 1; fi
if [ "$skip" -gt 0 ]; then echo "RESULT: passed, but $skip step(s) were skipped (see above)"; exit 0; fi
echo "RESULT: ALL PASSED"; exit 0
