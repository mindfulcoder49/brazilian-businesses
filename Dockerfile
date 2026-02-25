FROM python:3.12-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install Python dependencies first (layer cache)
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./backend/

# Copy frontend
COPY frontend/ ./frontend/

# Data volume mount point
RUN mkdir -p /data/logs

WORKDIR /app/backend

# Environment defaults (overridden by fly.toml secrets/env)
ENV DATABASE_PATH=/data/candidates.db
ENV LOG_PATH=/data/logs
ENV FRONTEND_DIR=../frontend

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
