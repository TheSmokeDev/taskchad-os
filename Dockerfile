# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# System deps (libgomp1 required by onnxruntime for fastembed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libgomp1 curl && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash botuser

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install supercronic for container-safe cron (scheduler service)
ADD https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

WORKDIR /app

# Copy dependency files first (cache layer)
COPY .claude/scripts/pyproject.toml .claude/scripts/uv.lock* ./scripts/
RUN cd scripts && uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

# Pre-download ONNX embedding model during build (~80MB)
RUN cd scripts && uv run python -c "from fastembed import TextEmbedding; TextEmbedding('all-MiniLM-L6-v2')" || true

# Copy application code
COPY .claude/chat/ ./chat/
COPY .claude/scripts/ ./scripts/
COPY .claude/data/ ./data/
# Copy vault (mount RW in production for background jobs)
COPY vault/memory/ ./memory/

# Copy cron entrypoint
COPY .claude/scripts/cron_entrypoint.sh /app/cron_entrypoint.sh
RUN chmod +x /app/cron_entrypoint.sh

# Ensure data directories exist
RUN mkdir -p data/state data/models && \
    chown -R botuser:botuser /app

USER botuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

EXPOSE 8787
EXPOSE 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8787/health')" || exit 1

# Default: run bot. Override entrypoint for scheduler service.
ENTRYPOINT ["python", "chat/main.py", "--fg"]
