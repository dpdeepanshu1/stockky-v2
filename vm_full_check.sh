#!/usr/bin/env bash
# vm_full_check.sh - one script that runs EVERYTHING CI runs, plus runtime checks, on the Ubuntu VM.
# Run from the repo root (e.g. ~/stockky-v2):   bash vm_full_check.sh            (all sections)
# Modes: all | quick | light (light = 00,01,02,60,70 only: small, safe while the market is open).
# It never restarts, builds or prunes anything. Section 10/30 use scripts/docker_test.sh (throw-away containers).
# Output: ~/vmtests/<section>.txt (full logs), ~/vmtests_summary.txt (short), ~/vmtests.tgz (everything in one file).
set -u
MODE="${1:-all}"
REPO="$(pwd)"
[ -f "$REPO/scripts/docker_test.sh" ] || { echo "Run this from the repo root (the folder that contains scripts/ and services/)."; exit 2; }
OUT="$HOME/vmtests"; SUM="$HOME/vmtests_summary.txt"
mkdir -p "$OUT"; rm -f "$OUT"/*.txt "$SUM" "$HOME/vmtests.tgz"
RESULTS=()
export PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10

step() {
  local name="$1"; shift
  echo "=== $name"
  local t0=$SECONDS
  { "$@"; } > "$OUT/$name.txt" 2>&1
  local rc=$?
  echo "exit=$rc" >> "$OUT/$name.txt"
  RESULTS+=("$(printf '%-42s exit=%-4s seconds=%s' "$name" "$rc" "$((SECONDS-t0))")")
}
want() { [ "$MODE" = all ] || [[ " $* " == *" $MODE "* ]]; }
dk() { docker run --rm -v "$REPO":/src:ro "$@"; }

s00() {
  echo "--- date/host"; date; hostname; uname -a; cat /etc/os-release | head -3
  echo "--- cpu/memory"; nproc; free -h
  echo "--- disk"; df -h / ; docker system df 2>&1
  echo "--- docker"; docker --version; docker compose version 2>&1 | head -1; docker info --format 'root={{.DockerRootDir}} containers={{.Containers}} running={{.ContainersRunning}}' 2>&1
  echo "--- python/node on host"; python3 --version; node --version 2>&1; npm --version 2>&1
  echo "--- repo"; pwd; ls
  echo "--- round deployed here"; head -c 600 CHANGELOG.md | head -5
  echo "--- git"; git log -1 --oneline 2>&1 | head -1; git status --short 2>&1 | head -20
  echo "--- is scripts/docker_test.sh executable?"; ls -l scripts/docker_test.sh
  echo "--- python image tags used by Dockerfiles"; for s in services/*/Dockerfile; do echo "$s: $(grep -m1 '^FROM' $s)"; done
}
step 00_environment s00

s01() { dk python:3.11-slim sh -c '
  export PYTHONDONTWRITEBYTECODE=1
  pip install -q --root-user-action=ignore pyyaml pytest >/dev/null 2>&1
  cp -r /src /work && cd /work
  for c in check_merge_conflicts check_shared_copies check_ms_package check_holiday_lists_sync check_table_name_collisions \
           check_price_source_imports check_requirements_drift check_control_catalog check_compose_gateway_url \
           check_compose_ports_loopback check_market_data_url_default check_compose_fno_env; do
    echo "--- $c"; python3 scripts/$c.py; echo "rc=$?"
  done
  echo "--- gen_env_reference --check"; python3 scripts/gen_env_reference.py --check; echo "rc=$?"'; }
want all quick light && step 01_repo_checks s01

s02() {
  list=$(grep -m1 "python -m pytest scripts/test_repo_scripts.py" .github/workflows/service-tests.yml | sed -E 's/.*pytest (.*) -q.*/\1/')
  echo "files: $(echo $list | wc -w)"
  docker run --rm -v "$REPO":/src:ro -e LIST="$list" python:3.11-slim sh -c '
    export PYTHONDONTWRITEBYTECODE=1
    pip install -q --root-user-action=ignore pytest numpy pandas fastapi httpx pydantic pyyaml >/dev/null 2>&1
    (apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1); git config --global user.email ci@example.com; git config --global user.name ci
    cp -r /src /work && cd /work
    python -m pytest $LIST -q -p no:cacheprovider -rfE --tb=short'
}
want all quick light && step 02_repo_scripts_tests s02

if want all; then
  for s in analysis-intelligence-service api-gateway market-data-service fno-options-service notification-scheduler-service position-stocks-service real-trade-service; do
    step "10_svc_$s" bash scripts/docker_test.sh "$s" tests -q -p no:cacheprovider -rfE --tb=short
  done
fi

if want all quick; then
  for d in training prediction decision; do
    step "20_decision_$d" bash scripts/docker_test.sh decision-prediction-service "$d/tests" -q -p no:cacheprovider -rfE --tb=short
  done
fi

s30() {
  docker run --rm -v "$REPO":/repo -v stockky-pip-cache:/root/.cache/pip -w "/repo/services/$1" \
    -e PIP_DEFAULT_TIMEOUT=120 -e PIP_RETRIES=10 python:3.12-slim sh -c '
    c=""; [ -f constraints.txt ] && c="-c constraints.txt"
    pip install -q --root-user-action=ignore -r requirements.txt $c pytest pytest-asyncio pytest-timeout >/dev/null || { echo "pip install failed"; exit 4; }
    python -m pytest tests -q -p no:cacheprovider --timeout=120 -rfE --tb=short'
}
if want all; then
  for s in position-stocks-service real-trade-service; do step "30_py312_$s" s30 "$s"; done
fi

s40a() { dk -w /src/services/calibration-runner-service python:3.11-slim sh -c '
  pip install -q --root-user-action=ignore pytest >/dev/null
  for t in tests/test_runner_engine.py tests/test_notify.py tests/test_autocalib.py; do echo "--- $t"; python $t; echo "rc=$?"; done
  for t in tests/test_group478_dockerfile_copies_modules.py tests/test_group479_runner_image_has_script_files.py; do echo "--- $t"; python -m pytest -q -p no:cacheprovider $t; echo "rc=$?"; done'; }
s40b() { dk -w /src/services/kronos-forecast-service python:3.11-slim sh -c '
  pip install -q --root-user-action=ignore pytest numpy pandas >/dev/null && python -m pytest tests -q -p no:cacheprovider -rfE --tb=short'; }
s40c() { dk -w /src/services/timesfm-forecast-service python:3.11-slim sh -c '
  pip install -q --root-user-action=ignore pytest fastapi httpx pydantic numpy >/dev/null && python -m pytest tests -q -p no:cacheprovider -rfE --tb=short'; }
if want all quick; then
  step 40_calibration_runner_stdlib s40a
  step 40_kronos_light s40b
  step 40_timesfm_light s40c
fi

s50() { dk node:22 bash -o pipefail -c '
  mkdir /app && cp -r /src/frontend/. /app/ && cd /app
  echo "--- npm ci";   npm ci --no-audit --no-fund 2>&1 | tail -5; echo "ci_exit=${PIPESTATUS[0]}"
  echo "--- npm test"; npm test 2>&1 | tail -40;                   echo "test_exit=${PIPESTATUS[0]}"
  echo "--- npm run build"; npm run build 2>&1 | tail -25;          echo "build_exit=${PIPESTATUS[0]}"'; }
want all quick && step 50_frontend s50

s60() { docker compose config -q && echo "compose config OK"; docker compose config --services; }
want all quick light && step 60_compose_config s60

s70() {
  echo "--- docker compose ps"; docker compose ps 2>&1
  echo "--- unhealthy / restarting / exited"; docker ps -a --filter health=unhealthy --filter status=restarting --filter status=exited --format '{{.Names}}  {{.Status}}'
  echo "--- restart counts"; for c in $(docker ps -q); do docker inspect -f '{{.Name}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}' $c; done
  echo "--- health endpoints (host loopback ports)"
  for p in 8000:api-gateway 8001:market-data 8002:analysis-intel 8004:decision-prediction 8005:real-trade 8006:position-stocks 8008:notification-scheduler 8011:timesfm; do
    port=${p%%:*}; n=${p##*:}
    printf '%-26s %s\n' "$n" "$(curl -s -o /dev/null -m 8 -w 'HTTP %{http_code} in %{time_total}s' http://127.0.0.1:$port/health 2>&1)"
  done
  echo "--- resource use"; docker stats --no-stream --format '{{.Name}}  cpu={{.CPUPerc}}  mem={{.MemUsage}}' 2>&1
  echo "--- errors in last 2h of logs (count per container; ERROR/Traceback/CRITICAL)"
  for c in $(docker ps --format '{{.Names}}'); do printf '%-45s %s\n' "$c" "$(docker logs --since 2h $c 2>&1 | grep -ciE 'error|traceback|critical')"; done
  echo "--- last 15 error lines per container"
  for c in $(docker ps --format '{{.Names}}'); do echo "## $c"; docker logs --since 2h $c 2>&1 | grep -iE 'error|traceback|critical' | tail -15 | cut -c1-300; done
  echo "--- volumes"; docker volume ls --format '{{.Name}}' | grep -E 'runner-data|bar-records|event-state|control-state|fno-data'
  echo "--- disk after"; df -h /
}
want all quick light && step 70_runtime s70

{
  echo "vm_full_check summary  $(date)   mode=$MODE   repo=$REPO"
  head -c 400 CHANGELOG.md | head -3
  echo; echo "===== exit codes (0 = green; 3 = not started, disk full; 4 = pip install failed; 126 = not executable; 127 = command not found)"
  printf '%s\n' "${RESULTS[@]}"
  echo
  for f in "$OUT"/*.txt; do
    echo "##### $(basename "$f" .txt)"
    grep -E "^(FAILED|ERROR) |passed|failed|error|exit=|rc=[1-9]|_exit=|HTTP |unhealthy|restarts=[1-9]|No space|NOT STARTED|Traceback|ModuleNotFound" "$f" | grep -v "^exit=0$" | tail -n 40
    grep -m1 "^exit=" "$f"
  done
} > "$SUM"
tar czf "$HOME/vmtests.tgz" -C "$HOME" vmtests vmtests_summary.txt 2>/dev/null
echo; echo "DONE. Paste the output of:   cat ~/vmtests_summary.txt"
echo "(full logs: ~/vmtests/*.txt , all in one file: ~/vmtests.tgz)"
