# Agent BFF — FastAPI + uvicorn on Cloud Run.
# Built/pushed via Cloud Build (see docs/PHASE3_OPS_RUNBOOK.md 3a.1).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App (static UI + BFF). .dockerignore keeps junk out.
COPY . .

# Cloud Run injects $PORT (default 8080). Bind it, no --reload in the container.
ENV PORT=8080
EXPOSE 8080
# Shell form so $PORT expands at runtime.
CMD exec uvicorn app.server:app --host 0.0.0.0 --port ${PORT}
