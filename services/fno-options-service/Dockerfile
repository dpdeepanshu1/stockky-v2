FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# a syntax error fails the image build instead of crash-looping the container
RUN python -m compileall -q -x '(^|/)(tests?|\.venv[^/]*|venv|node_modules)(/|$)' .

ENV PORT=8007
EXPOSE 8007
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8007"]
