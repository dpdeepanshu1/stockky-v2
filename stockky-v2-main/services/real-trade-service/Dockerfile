FROM python:3.11-slim

WORKDIR /app

# 2026-09-07: curl added for ops debugging (see market-data-service's
# Dockerfile for why) — same container-debugging gap applies here.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8005
EXPOSE $PORT

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]
