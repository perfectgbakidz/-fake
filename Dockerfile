FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/health.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN mkdir -p /data

EXPOSE 8000

# Render injects $PORT, so fall back to 8000 for local runs
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
