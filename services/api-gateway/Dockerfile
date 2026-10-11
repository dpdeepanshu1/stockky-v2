FROM python:3.11-slim
WORKDIR /app
# group313: constraints.txt (made by scripts/freeze_constraints.sh) pins every dependency to the versions the tests ran on; optional until generated
COPY requirements.txt constraints.tx[t] ./
RUN if [ -f constraints.txt ]; then pip install --no-cache-dir -r requirements.txt -c constraints.txt; else pip install --no-cache-dir -r requirements.txt; fi
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
