FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Uploaded meetings live in /app/data (mounted from the host).
RUN mkdir -p /app/data

# Single worker: per-memo upload locks are process-local by design.
CMD ["python", "-m", "app.main"]
