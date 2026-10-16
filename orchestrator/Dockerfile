FROM python:3.13-slim

WORKDIR /app

# Hosted-volume snapshot helpers execute inside this controlled orchestrator
# image, not inside whichever sandbox image happens to be old or incomplete.
# These are the verification/copy tools the helper invokes directly. git is for
# the unused-home inspector (orchestrator/empty_home.py), which judges a
# template-seeded clone in a home by its own git state.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        acl \
        coreutils \
        findutils \
        git \
        tar \
    && rm -rf /var/lib/apt/lists/*

ARG UV_VERSION=0.10.8
ARG MATRX_SOURCE_SHA=dev
COPY pyproject.toml uv.lock ./
# Resolve only from the reviewed lockfile. The project itself is installed
# after source is copied so dependency layers remain cacheable.
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev
# Internal packages are always the latest published (never pinned by the lock): matrx-utils carries the
# shared config-attestation implementation (common-docs/systems/platform/app-config/attestation/FEATURE.md).
RUN uv pip install --python /app/.venv/bin/python --upgrade matrx-utils
RUN install -d -m 0700 /var/lib/matrx-sandbox/hosted-migrations
RUN printf '%s\n' "${MATRX_SOURCE_SHA}" > /app/.source-sha
# Config attestation, DECLARED half: the manifest of every knob this image's code can read, generated from
# these exact bytes for MATRX_SOURCE_SHA and attested by the orchestrator at boot (orchestrator/config_attestation.py),
# so the attested sha is the running sha on EC2 and hosted alike. Never fails the build: no manifest = the
# orchestrator reports "NOT attested" loudly at boot and on /health/config-attestation.
RUN rm -f /app/.config-manifest.json \
    && if ! printf '%s' "${MATRX_SOURCE_SHA}" | grep -Eq '^[0-9a-f]{40}$'; then \
        echo "WARNING: MATRX_SOURCE_SHA '${MATRX_SOURCE_SHA}' is not a 40-hex sha; config manifest NOT baked"; \
    elif ! /app/.venv/bin/python scripts/config_manifest.py --root /app --build-sha "${MATRX_SOURCE_SHA}" --out /app/.config-manifest.json; then \
        echo "WARNING: config manifest generation FAILED for ${MATRX_SOURCE_SHA}; this build will report unattested"; \
    fi

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
