FROM python:3.11-slim

WORKDIR /app

ARG UV_VERSION=0.10.8
ARG MATRX_SOURCE_SHA=dev
COPY pyproject.toml uv.lock ./
# Resolve only from the reviewed lockfile. The project itself is installed
# after source is copied so dependency layers remain cacheable.
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev
RUN printf '%s\n' "${MATRX_SOURCE_SHA}" > /app/.source-sha

ENV PATH="/app/.venv/bin:${PATH}"
LABEL com.aimatrx.source.sha="${MATRX_SOURCE_SHA}"

EXPOSE 8000

# IMPORTANT: single worker, no --reload.
#   * --reload watches the filesystem (dev-only; restarts silently wipe all
#     in-process state and waste CPU in prod).
#   * The orchestrator keeps process-local state (sandbox CWD cache, activity /
#     migration gating, warm-pool bookkeeping, single-use token JTIs). Running
#     more than one worker would shard that state and silently break migration
#     gating and warm-pool claims. Do NOT add --workers N without first moving
#     that state to a shared store (Postgres/Redis).
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
