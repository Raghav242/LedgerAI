# syntax=docker/dockerfile:1
# LedgerFlow AI images. Two targets from one Dockerfile:
#   api (default, last stage): every API replica, the event workers and the migrate job
#   ai:                        the AI anomaly worker (adds scikit-learn; the API never needs it)

FROM python:3.13-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Unprivileged runtime user.
RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app

# Exact, pinned versions (generated on this same base image; see README) -> reproducible.
# Copied before the source so dependency layers are cached across code changes.
COPY requirements.lock ./
RUN pip install --root-user-action=ignore -r requirements.lock


# --- AI worker -------------------------------------------------------------------------------
FROM deps AS ai
COPY requirements-ai.lock ./
RUN pip install --root-user-action=ignore -r requirements-ai.lock
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
USER app
CMD ["python", "-m", "app.ai.worker"]


# --- API (default target) -------------------------------------------------------------------
FROM deps AS api
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

USER app
EXPOSE 8000

# Liveness only (no dependency checks): see /health/live vs /health/ready.
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"]

# No secrets in the image: all configuration comes from the environment at run time.
# --proxy-headers: trust X-Forwarded-* from NGINX (the API port is not published; only
# NGINX on the internal network can reach it).
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header", \
     "--timeout-graceful-shutdown", "10"]
