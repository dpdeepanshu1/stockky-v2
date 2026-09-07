FROM python:3.11-slim
WORKDIR /app
# 2026-09-07: curl added for ops debugging — session21 needed it inside the
# running container ("exec: curl: executable file not found in $PATH") and
# had to fall back to python heredocs instead. /angelone/network-check and
# /dhan/network-check now cover the routine IP-debugging case without curl,
# but it's still handy for ad hoc checks.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
