FROM python:3.11-slim
WORKDIR /app
# CPU-only torch keeps the image ~1 GB smaller than the default CUDA wheel
COPY requirements.txt ./
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m compileall -q -x '(^|/)(tests?|\.venv[^/]*|venv|node_modules)(/|$)' .
ENV PORT=8010 HF_HOME=/app/.hf
EXPOSE 8010
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]
