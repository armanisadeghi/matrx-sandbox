FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
# Production image: runtime deps only (no ".[dev]" — no pytest/moto), and no
# editable install. Local development restores hot-reload + the bind mount via
# a `command:` override in docker-compose.yml.
RUN pip install --no-cache-dir .

COPY . .

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
