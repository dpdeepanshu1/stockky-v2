# group 442: calibration-runner-service. BUILD CONTEXT IS THE REPO ROOT (it runs the repo's own scripts/ and shared/):
#   docker compose build calibration-runner-service      (compose sets context: . and dockerfile: services/calibration-runner-service/Dockerfile)
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl bash \
    && rm -rf /var/lib/apt/lists/*

# group449: constraints.txt (made by scripts/freeze_constraints.sh) pins every dependency to the tested versions; optional until generated
COPY services/calibration-runner-service/requirements.txt services/calibration-runner-service/constraints.tx[t] ./
RUN if [ -f constraints.txt ]; then pip install --no-cache-dir -r requirements.txt -c constraints.txt; else pip install --no-cache-dir -r requirements.txt; fi

# the service itself
COPY services/calibration-runner-service/config.py services/calibration-runner-service/auth.py services/calibration-runner-service/jobs.py \
     services/calibration-runner-service/runner.py services/calibration-runner-service/scheduler.py services/calibration-runner-service/main.py \
     services/calibration-runner-service/notify.py services/calibration-runner-service/autocalib.py \
     services/calibration-runner-service/weather.py services/calibration-runner-service/weather_apply.py services/calibration-runner-service/weather_overlay.py \
     services/calibration-runner-service/session_keys.py ./

# Market Weather (Part 1 step 3): the regime/ package the replay script imports (pure computation)
COPY services/calibration-runner-service/regime /app/regime

# what the scripts import: the shared structure engine, and for Kronos the real-trade gate (config.py + entry_engine/kronos_gate.py only)
COPY scripts /app/scripts
COPY shared /app/shared
COPY services/real-trade-service/config.py /app/services/real-trade-service/config.py
# E131: scripts/swing_backtest.py scores with the SAME pure module the live /swing/rank uses (stdlib only)
COPY services/real-trade-service/swing/__init__.py services/real-trade-service/swing/momentum.py services/real-trade-service/swing/stats.py /app/services/real-trade-service/swing/
COPY services/real-trade-service/entry_engine/__init__.py services/real-trade-service/entry_engine/kronos_gate.py /app/services/real-trade-service/entry_engine/

RUN python -m compileall -q -x '(^|/)(tests?|\.venv[^/]*|venv|node_modules)(/|$)' . /app/scripts /app/shared \
    && mkdir -p /data/runner

ENV PORT=8012 RUNNER_DATA_DIR=/data/runner RUNNER_SCRIPTS_DIR=/app/scripts PYTHONUNBUFFERED=1
EXPOSE 8012

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8012"]
