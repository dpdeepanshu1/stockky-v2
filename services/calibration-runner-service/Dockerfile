# group 442: calibration-runner-service. BUILD CONTEXT IS THE REPO ROOT (it runs the repo's own scripts/ and shared/):
#   docker compose build calibration-runner-service      (compose sets context: . and dockerfile: services/calibration-runner-service/Dockerfile)
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl bash \
    && rm -rf /var/lib/apt/lists/*

COPY services/calibration-runner-service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# the service itself
COPY services/calibration-runner-service/config.py services/calibration-runner-service/auth.py services/calibration-runner-service/jobs.py \
     services/calibration-runner-service/runner.py services/calibration-runner-service/scheduler.py services/calibration-runner-service/main.py \
     services/calibration-runner-service/notify.py ./

# what the scripts import: the shared structure engine, and for Kronos the real-trade gate (config.py + entry_engine/kronos_gate.py only)
COPY scripts /app/scripts
COPY shared /app/shared
COPY services/real-trade-service/config.py /app/services/real-trade-service/config.py
COPY services/real-trade-service/entry_engine/__init__.py services/real-trade-service/entry_engine/kronos_gate.py /app/services/real-trade-service/entry_engine/

RUN python -m compileall -q -x '(^|/)(tests?|\.venv|node_modules)(/|$)' . /app/scripts /app/shared \
    && mkdir -p /data/runner

ENV PORT=8012 RUNNER_DATA_DIR=/data/runner RUNNER_SCRIPTS_DIR=/app/scripts PYTHONUNBUFFERED=1
EXPOSE 8012

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8012"]
