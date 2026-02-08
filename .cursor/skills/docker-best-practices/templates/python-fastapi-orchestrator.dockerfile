# syntax=docker/dockerfile:1.7
#
# Production-grade FastAPI orchestrator
# Optimized for: Fast builds, minimal size, security
#
# Build: docker build -f templates/python-fastapi-orchestrator.dockerfile -t orchestrator:latest .
# Dev:   docker build --target development -t orchestrator:dev .
# Prod:  docker build --target production -t orchestrator:prod .

# ─── Base stage — shared dependencies ───────────────────────────────────────
FROM python:3.11.11-slim AS base

WORKDIR /app

# Install system dependencies (minimal runtime deps only)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -g 1000 app && \
    useradd -m -u 1000 -g app -s /bin/bash app && \
    chown -R app:app /app

# ─── Builder stage — installs dependencies ──────────────────────────────────
FROM base AS builder

# Install build dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install uv (blazing fast package installer)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --upgrade pip setuptools wheel uv

# Copy only dependency files (for layer caching)
COPY pyproject.toml uv.lock* ./

# Install dependencies with cache mount
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system .

# ─── Development stage — includes dev tools and hot reload ──────────────────
FROM base AS development

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Install dev dependencies
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system ".[dev]"

# Copy application code
COPY --chown=app:app . .

USER app
EXPOSE 8000

# Hot reload enabled for development
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ─── Production stage — minimal runtime image ───────────────────────────────
FROM base AS production

# Copy installed packages from builder (excludes build tools)
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=app:app orchestrator/ orchestrator/
COPY --chown=app:app migrations/ migrations/

# Switch to non-root user
USER app

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=2)" || exit 1

EXPOSE 8000

# Production server with optimized settings
CMD ["uvicorn", "orchestrator.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--log-level", "info", \
     "--no-access-log"]
