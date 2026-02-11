# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# Matrx Sandbox — Local Variant
# ─────────────────────────────────────────────────────────────────────────────
# Extends the core sandbox image for non-AWS, self-hosted Docker environments.
#
# Differences from core:
#   - Skips S3 hot/cold storage sync (uses local Docker volumes instead)
#   - Adds ttyd for browser-based web terminal access
#   - Lighter healthcheck (no FUSE mount verification)
#   - Custom entrypoint for local-mode startup
#
# Build:
#   docker build -t matrx-sandbox:local sandbox-local/
#
# Requires core image built first:
#   docker build -t matrx-sandbox:core sandbox-image/
# ─────────────────────────────────────────────────────────────────────────────

ARG CORE_VERSION=core
FROM matrx-sandbox:${CORE_VERSION}

LABEL variant="local"
LABEL description="Self-hosted sandbox with web terminal — no AWS dependencies"

USER root

# ─── Install ttyd (web-based terminal) ────────────────────────────────────────
# ttyd shares a terminal via HTTP — gives each sandbox a browser-accessible shell
RUN UARCH=$(uname -m) \
    && if [ "$UARCH" = "x86_64" ]; then TTYD_ARCH="x86_64"; \
       elif [ "$UARCH" = "aarch64" ]; then TTYD_ARCH="aarch64"; \
       else echo "Unsupported arch: $UARCH" && exit 1; fi \
    && curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${TTYD_ARCH}" \
       -o /usr/local/bin/ttyd \
    && chmod +x /usr/local/bin/ttyd

# ─── Copy local-mode scripts ─────────────────────────────────────────────────
COPY scripts/entrypoint-local.sh /opt/sandbox/scripts/entrypoint-local.sh
COPY scripts/healthcheck-local.sh /opt/sandbox/scripts/healthcheck-local.sh
COPY scripts/shutdown-local.sh /opt/sandbox/scripts/shutdown-local.sh
RUN chmod +x /opt/sandbox/scripts/entrypoint-local.sh \
             /opt/sandbox/scripts/healthcheck-local.sh \
             /opt/sandbox/scripts/shutdown-local.sh

# ─── Override environment defaults for local mode ─────────────────────────────
# S3_BUCKET left empty — entrypoint detects local mode from this
ENV S3_BUCKET=""
ENV S3_REGION=""
ENV SANDBOX_MODE="local"

# ─── Expose ttyd web terminal port ──────────────────────────────────────────
EXPOSE 7681

# ─── Healthcheck for local mode ──────────────────────────────────────────────
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD /opt/sandbox/scripts/healthcheck-local.sh

# ─── Use tini as init, launch local entrypoint ──────────────────────────────
ENTRYPOINT ["tini", "--"]
CMD ["/opt/sandbox/scripts/entrypoint-local.sh"]
