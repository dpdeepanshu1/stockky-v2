FROM python:3.11-slim

WORKDIR /app

# 2026-09-07: curl added for ops debugging (see market-data-service's
# Dockerfile for why) — same container-debugging gap applies here.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# group313: constraints.txt (made by scripts/freeze_constraints.sh) pins every dependency to the versions the tests ran on; optional until generated
COPY requirements.txt constraints.tx[t] ./
RUN if [ -f constraints.txt ]; then pip install --no-cache-dir -r requirements.txt -c constraints.txt; else pip install --no-cache-dir -r requirements.txt; fi

COPY . .

# group341c: a syntax error (e.g. a leftover merge marker) fails the image build instead of crash-looping the container
RUN python -m compileall -q -x '(^|/)(tests?|\.venv|node_modules)(/|$)' .

ENV PORT=8005
EXPOSE $PORT

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]
