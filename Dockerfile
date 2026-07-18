FROM python:3.12-slim

WORKDIR /app

# Unbuffered stdout/stderr so log lines reach `docker logs` in real time.
ENV PYTHONUNBUFFERED=1

# psycopg2 (sync driver used by Alembic's PostgreSQL path) needs libpq.
# Install before pip so the layer is cached independently of code changes.
RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first so src/ changes don't bust the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source.
COPY src ./src

# Alembic schema migrations — applied on startup by init_schema().
COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8000

# SERVICE env var selects which process to run:
#   SERVICE=agent     (default) — webhook receiver + pipeline + background tasks
#   SERVICE=dashboard           — read-only UI + API
#
# Both pods use the same image; the Helm chart sets SERVICE per-Deployment.
CMD if [ "${SERVICE:-agent}" = "dashboard" ]; then \
        uvicorn src.dashboard.main:app --host 0.0.0.0 --port 8000; \
    else \
        uvicorn src.api.main:app --host 0.0.0.0 --port 8000; \
    fi
