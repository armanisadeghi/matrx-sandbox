# syntax=docker/dockerfile:1.7
#
# Multi-runtime sandbox for AI agent execution
# Includes: Python 3.11, Node.js 20, AWS CLI, FUSE, Chromium
# Optimized for: Fast startup, efficient layer caching
#
# Build: docker build -f templates/sandbox-multi-runtime.dockerfile -t sandbox:latest .
# Multi-platform: docker buildx build --platform linux/amd64,linux/arm64 -t sandbox:latest .

FROM ubuntu:22.04.5 AS base

LABEL maintainer="AI Matrx <team@aimatrx.com>"
LABEL description="Ephemeral sandbox for AI agent execution"

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# ─── Core system packages (single cached layer) ─────────────────────────────
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    # Core utilities
    bash curl wget git jq unzip zip tar gzip ca-certificates gnupg lsb-release \
    # Text tools
    less vim-tiny ripgrep fd-find tree file \
    # Process management
    htop tmux tini \
    # Build essentials (for pip packages with native extensions)
    build-essential libffi-dev libssl-dev \
    # FUSE for S3 cold storage mount
    fuse libfuse2 \
    # Network tools
    dnsutils iputils-ping net-tools openssh-client \
    # Python 3.11
    python3.11 python3.11-venv python3.11-dev python3-pip \
    # Node.js dependencies (nodesource script installs nodejs separately)
    && ln -sf /usr/bin/fdfind /usr/bin/fd \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && rm -rf /var/lib/apt/lists/*

# ─── Python setup ────────────────────────────────────────────────────────────
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel uv

# ─── Node.js 20 LTS ──────────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/* && \
    npm install -g npm@latest

# ─── AWS CLI v2 (multi-arch) ─────────────────────────────────────────────────
RUN UARCH=$(uname -m) && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-${UARCH}.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip aws/

# ─── Mountpoint for Amazon S3 (FUSE driver for cold storage) ────────────────
# Only available on x86_64 — ARM64 builds skip (local dev on Apple Silicon)
RUN UARCH=$(uname -m) && \
    if [ "$UARCH" = "x86_64" ]; then \
        curl -LO "https://s3.amazonaws.com/mountpoint-s3-release/latest/x86_64/mount-s3.deb" && \
        dpkg -i mount-s3.deb && \
        rm mount-s3.deb; \
    else \
        echo "⚠️  Skipping mountpoint-s3 (not available for $UARCH)"; \
    fi

# ─── Chromium (headless browser for web access) ─────────────────────────────
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    chromium-browser fonts-liberation libnss3 libxss1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright (reuses system Chromium)
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip install --no-cache-dir playwright && \
    playwright install-deps

# ─── Common Python packages for agent use ───────────────────────────────────
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip install --no-cache-dir \
    httpx requests aiohttp pydantic rich click \
    pyyaml toml python-dotenv beautifulsoup4 lxml \
    pandas numpy

# ─── Create agent user (non-root) ───────────────────────────────────────────
RUN groupadd -g 1000 agent && \
    useradd -m -u 1000 -g agent -s /bin/bash agent && \
    mkdir -p /home/agent /data/cold /var/log/sandbox && \
    chown -R agent:agent /home/agent /data/cold /var/log/sandbox

# Allow non-root user to use FUSE
RUN echo "user_allow_other" >> /etc/fuse.conf

# ─── Copy sandbox scripts ───────────────────────────────────────────────────
COPY --chown=agent:agent scripts/ /opt/sandbox/scripts/
RUN chmod +x /opt/sandbox/scripts/*.sh

# ─── Copy sandbox config ────────────────────────────────────────────────────
COPY --chown=agent:agent config/ /opt/sandbox/config/

# ─── Install Matrx agent SDK ────────────────────────────────────────────────
COPY --chown=agent:agent sdk/ /opt/sandbox/sdk/
RUN --mount=type=cache,target=/root/.cache/pip \
    cd /opt/sandbox/sdk && python3 -m pip install --no-cache-dir -e .

# ─── Environment variables (defaults, overridden at runtime) ────────────────
ENV SANDBOX_ID="" \
    USER_ID="" \
    S3_BUCKET="" \
    S3_REGION="us-east-1" \
    HOT_PATH="/home/agent" \
    COLD_PATH="/data/cold" \
    SHUTDOWN_TIMEOUT_SECONDS="30"

# ─── Health check ───────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD /opt/sandbox/scripts/healthcheck.sh || exit 1

# Switch to agent user
USER agent
WORKDIR /home/agent

# Use tini as init to handle signals properly
ENTRYPOINT ["tini", "--"]
CMD ["/opt/sandbox/scripts/entrypoint.sh"]
