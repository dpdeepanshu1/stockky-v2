FROM python:3.11-slim
WORKDIR /app
# TSFM_INSTALL=timesfm25 (default): CPU torch + the upstream timesfm package (needs github.com at build time; ~2 GB image).
# TSFM_INSTALL=bigquery: google-cloud-bigquery only (small image).  TSFM_INSTALL=none: pure service (fake backend / wiring checks).
# UNVERIFIED in the build sandbox (no network to those hosts): the first `docker compose build` on the VM is the real test.
ARG TSFM_INSTALL=timesfm25
ARG TSFM_PIP="timesfm[torch] @ git+https://github.com/google-research/timesfm.git"
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$TSFM_INSTALL" = "timesfm25" ]; then \
         pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && pip install --no-cache-dir "$TSFM_PIP"; \
       elif [ "$TSFM_INSTALL" = "bigquery" ]; then pip install --no-cache-dir google-cloud-bigquery; fi
COPY . .
RUN python -m compileall -q -x '(^|/)(tests?|\.venv)(/|$)' .
ENV PORT=8011 HF_HOME=/app/.hf TSFM_CACHE_PATH=/app/cache/tsfm_cache.json
EXPOSE 8011
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8011"]
