# syntax=docker/dockerfile:1.7
# Single image — both the api and dashboard services run from this same image,
# differentiated by the docker-compose service-level command. One build, one
# layer cache, one place to update Python deps.
FROM python:3.13-slim AS base

# Non-root user for runtime — limits blast radius if either service is owned.
RUN useradd --create-home --shell /bin/bash --uid 1000 app

WORKDIR /srv/app

# Install deps in their own layer so source edits don't bust the dep cache.
# Copy only pyproject first, install, then copy the rest.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[dashboard]"

# Copy the source. .dockerignore keeps .venv, __pycache__, data, .env etc out.
COPY app/ ./app/
COPY dashboard/ ./dashboard/
COPY scripts/ ./scripts/

# Default DB path lives on a docker volume mounted at /data.
ENV DB_PATH=/data/automation.db
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Pre-create the data dir owned by the runtime user so volume mounts inherit
# correct permissions even when the host doesn't pre-create them.
RUN mkdir -p /data && chown -R app:app /data /srv/app

USER app
EXPOSE 8000 8501

# No CMD here — docker-compose specifies per-service. Forces operators to
# pick a service intentionally rather than getting a magic default.
