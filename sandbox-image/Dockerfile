FROM ubuntu:22.04

LABEL maintainer="AI Matrx <team@aimatrx.com>"
LABEL description="Ephemeral sandbox for AI agent execution"

# Prevent interactive prompts during package install
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# ─── System packages ─────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core utilities
    bash \
    curl \
    wget \
    git \
    jq \
    unzip \
    zip \
    tar \
    gzip \
    less \
    vim-tiny \
    htop \
    tmux \
    tree \
    file \
    ca-certificates \
    gnupg \
    lsb-release \
    # Build essentials (for pip packages with native extensions)
    build-essential \
    libffi-dev \
    libssl-dev \
    # FUSE for cold storage mount
    fuse \
    libfuse2 \
    # Network tools
    dnsutils \
    iputils-ping \
    net-tools \
    openssh-client \
    # Process management
    tini \
    # PDF text extraction (for Read tool PDF support)
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# ─── ripgrep and fd-find (multi-arch via apt) ────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep \
    fd-find \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/fdfind /usr/bin/fd

# ─── Python 3.12 — THE PYTHON RULE: a RELEASE build, never a candidate ───────
# Ubuntu 22.04's own `python3.11` package is **3.11.0~rc1**, a 2022 release
# candidate, and every sandbox ran pre-release CPython until the 2026-09-18
# field report (P2-1) caught it. `python3` must be a final release ≥ 3.12 in
# every variant; the build step below refuses to ship anything else.
#
# deadsnakes, via an explicit keyring — NOT `software-properties-common`. The
# "deadsnakes is brittle on 22.04" note in Dockerfile.aidream is about that
# package's python3-apt/dbus cascade; adding the keyring + one sources.list
# line never touches it.
#
# 3.11 is GONE, not kept beside 3.12 (no-legacy): anything still naming
# /usr/bin/python3.11 must fail loudly rather than quietly run an old
# interpreter without the SDK. In-container callers use /usr/bin/python3.
ARG PYTHON_VERSION=3.12
ARG DEADSNAKES_KEY=F23C5A6CF475977595C89F51BA6932366A755776
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${DEADSNAKES_KEY}" \
        | gpg --dearmor -o /etc/apt/keyrings/deadsnakes.gpg \
    && chmod go+r /etc/apt/keyrings/deadsnakes.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/deadsnakes.gpg] https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/deadsnakes.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        "python${PYTHON_VERSION}" \
        "python${PYTHON_VERSION}-venv" \
        "python${PYTHON_VERSION}-dev" \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python3 python3 "/usr/bin/python${PYTHON_VERSION}" 2 \
    && update-alternatives --install /usr/bin/python  python  "/usr/bin/python${PYTHON_VERSION}" 2 \
    && python3 --version \
    && python3 -c "import sys; v=sys.version_info; assert v[:2] >= (3, 12) and v.releaselevel == 'final', f'sandbox python3 must be a release >= 3.12, got {sys.version}'"

# pip comes from get-pip, not apt: the `python3-pip` package is wired to the
# distro interpreter we just replaced.
RUN curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py \
    && python3 /tmp/get-pip.py --no-cache-dir \
    && rm /tmp/get-pip.py \
    && python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# ─── Node.js 22 LTS + an npm the AGENT can actually use ──────────────────────
# Three defects from the 2026-09-18 field report are closed here as ONE
# contract, because they are one contract in the agent's hands:
#
#   P1-1  Node 20 is below the floor of the tools agents install. Stagehand v4
#         died with `ReferenceError: WebSocket is not defined` (needs Node >=22).
#   P1-2  `npm i -g <pkg>` as the agent user failed EACCES: the global prefix
#         was /usr, whose lib/node_modules is root-owned. The fix is an
#         IMAGE-owned prefix, /opt/npm-global, chowned to the agent uid — NOT
#         ~/.npm-global. The home is restored wholesale from a per-user volume
#         or S3 on boot, so a prefix inside it is either wiped or resurrected
#         stale across an image swap; /opt/npm-global is rebuilt with the image
#         and survives every home restore untouched.
#   P1-3  npm 11 ships an install-script allowlist (`allow-scripts`), so a
#         dependency's postinstall is SKIPPED with only a warning unless a
#         policy is set. Half of the native JS ecosystem (esbuild, sharp,
#         puppeteer, playwright) is broken by that silently.
#
# THE SCRIPT POLICY, stated: inside a Matrx sandbox the agent's own installs
# run their lifecycle scripts (`dangerously-allow-all-scripts=true`). The
# isolation boundary here is the container, not npm's allowlist — the box is
# single-tenant, disposable, already grants passwordless sudo, and carries no
# platform credentials (see the platform-env isolation rule in CLAUDE.md). An
# allowlist that a non-interactive agent cannot answer would only convert a
# loud install failure into a silently half-installed package. Documented in
# ADDING_UTILITIES.md § The toolchain contract.
ARG NODE_MAJOR=22
ENV NPM_CONFIG_PREFIX=/opt/npm-global
ENV PATH="/opt/npm-global/bin:${PATH}"
# uid/gid 1000 numerically — the `agent` user is created further down, and the
# prefix has to exist before the first global install in this file.
RUN install -d -o 1000 -g 1000 -m 0755 \
        /opt/npm-global /opt/npm-global/bin /opt/npm-global/lib /opt/npm-global/etc \
    && printf '%s\n' \
        '# Matrx sandbox npm policy — see sandbox-image/ADDING_UTILITIES.md.' \
        'dangerously-allow-all-scripts=true' \
        'ignore-scripts=false' \
        'fund=false' \
        > /opt/npm-global/etc/npmrc \
    && chown 1000:1000 /opt/npm-global/etc/npmrc
# sshd starts shells with none of the container env, so the prefix and PATH are
# published to every login shell too (same reason as /etc/matrx/bridge-env.sh).
RUN printf '%s\n' \
        '# Matrx sandbox: agent-writable npm global prefix (see ADDING_UTILITIES.md).' \
        'NPM_CONFIG_PREFIX=/opt/npm-global' \
        'export NPM_CONFIG_PREFIX' \
        'case ":$PATH:" in' \
        '    *:/opt/npm-global/bin:*) ;;' \
        '    *) PATH="/opt/npm-global/bin:$PATH"; export PATH ;;' \
        'esac' \
        > /etc/profile.d/matrx-npm-global.sh \
    && chmod 0644 /etc/profile.d/matrx-npm-global.sh
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    # npm pinned to major 11. NEVER @latest here: npm@12 dropped Node 20 and one
    # unpinned @latest broke every image build on both deploy pipelines for a
    # day (2026-07-08) the moment it was published.
    && npm install -g npm@11 \
    # corepack on, so a repo declaring `packageManager` resolves its own pnpm or
    # yarn without a download prompt. The pinned pnpm below still wins on PATH.
    && corepack enable \
    # npm creates lib/node_modules as root during this layer — hand the whole
    # prefix back to the agent or P1-2 comes straight back.
    && chown -R 1000:1000 /opt/npm-global \
    && node --version \
    && npm --version

# ─── Agent project toolchain: uv, pnpm, gh ───────────────────────────────────
# Every sandbox ships these — bare/core/node-22/python-3.13 all run this image.
# Reason (2026-09-14 agent-efficiency-loop ruling): with uv absent, an agent
# asked to set up a Python project burned 3-4 failed tool calls improvising
# `uv init` flags before falling back to venv+pip. `mtx new python <name>`
# (SDK CLI) is the recipe; it refuses to scaffold if these are missing.
#
# Pin every version. NEVER @latest here — one unpinned npm@latest broke every
# image build on both deploy pipelines for a day (2026-07-08); see the npm note
# above. Bump these ARGs deliberately.
ARG UV_VERSION=0.10.8
ARG PNPM_VERSION=10.15.0
# corepack is enabled in the Node layer above for repos that declare a
# `packageManager` field; we ALSO install a real pinned pnpm (first on PATH)
# so `pnpm` works offline and without a download prompt.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
        | env UV_INSTALL_DIR=/usr/local/bin sh \
    && npm install -g "pnpm@${PNPM_VERSION}" \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    # Fail the build loudly rather than shipping an image that silently lacks
    # the toolchain every agent now assumes.
    && uv --version \
    && pnpm --version \
    && gh --version

# ─── AWS CLI v2 (multi-arch) ─────────────────────────────────────────────────
RUN UARCH=$(uname -m) \
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-${UARCH}.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip aws/

# ─── Mountpoint for Amazon S3 (cold storage FUSE) ────────────────────────────
# Only available on x86_64 — ARM64 builds skip this (cold mount won't work locally)
RUN UARCH=$(uname -m) \
    && if [ "$UARCH" = "x86_64" ]; then \
         curl -LO "https://s3.amazonaws.com/mountpoint-s3-release/latest/x86_64/mount-s3.deb" \
         && dpkg -i mount-s3.deb \
         && rm mount-s3.deb; \
       else \
         echo "Skipping mountpoint-s3 (not available for $UARCH)"; \
       fi

# ─── SSH server + sudo (full access inside the sandbox) ──────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Configure sshd: key-only auth, allow agent + root
RUN mkdir -p /run/sshd \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && echo "AllowUsers agent root" >> /etc/ssh/sshd_config

# ─── Chromium (headless browser for agent web access) ─────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium-browser \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Install playwright for Python with Chromium browser binary
RUN python3 -m pip install --no-cache-dir playwright \
    && playwright install-deps \
    && playwright install chromium

# ─── Common Python packages for agent use ─────────────────────────────────────
RUN python3 -m pip install --no-cache-dir \
    httpx \
    requests \
    aiohttp \
    pydantic \
    rich \
    click \
    pyyaml \
    toml \
    python-dotenv \
    beautifulsoup4 \
    lxml \
    pandas \
    numpy

# ─── Create agent user (non-root) ────────────────────────────────────────────
RUN groupadd -g 1000 agent \
    && useradd -m -u 1000 -g agent -s /bin/bash agent \
    && mkdir -p /home/agent /data/cold /var/log/sandbox \
    && chown -R agent:agent /home/agent /data/cold /var/log/sandbox

# Give agent passwordless sudo — the container IS the security boundary
RUN echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

# Allow agent to use FUSE
RUN echo "user_allow_other" >> /etc/fuse.conf

# ─── Copy sandbox scripts ────────────────────────────────────────────────────
COPY scripts/ /opt/sandbox/scripts/
RUN chmod +x /opt/sandbox/scripts/*.sh

# ─── Copy sandbox config ─────────────────────────────────────────────────────
COPY config/ /opt/sandbox/config/

# ─── Set up SSH keys for admin access ────────────────────────────────────────
# Admin keys baked into image — team can SSH into any sandbox
RUN mkdir -p /home/agent/.ssh \
    && cp /opt/sandbox/config/admin_authorized_keys /home/agent/.ssh/authorized_keys \
    && chown -R agent:agent /home/agent/.ssh \
    && chmod 700 /home/agent/.ssh \
    && chmod 600 /home/agent/.ssh/authorized_keys \
    # Also set up root SSH for deep debugging
    && mkdir -p /root/.ssh \
    && cp /opt/sandbox/config/admin_authorized_keys /root/.ssh/authorized_keys \
    && chmod 700 /root/.ssh \
    && chmod 600 /root/.ssh/authorized_keys

# ─── Install Matrx SDK (agent utilities + Claude Code-compatible tools) ───────
COPY sdk/ /opt/sandbox/sdk/
RUN python3 -m pip install --no-cache-dir --require-hashes \
        -r /opt/sandbox/sdk/requirements.lock \
    && python3 -m pip install --no-cache-dir --no-deps -e /opt/sandbox/sdk

# ─── ``mtx`` CLI shim — users/agents type ``mtx <subcommand>`` in any shell ──
# Wraps ``python3 -m matrx_agent.cli`` so the SDK's CLI feels like a first-class
# tool. Subcommands: files (ls/cat/put/rm/sync), whoami. See
# /opt/sandbox/sdk/matrx_agent/cli/__init__.py for the auth + AI Dream details.
RUN printf '#!/bin/sh\nexec /usr/bin/python3 -m matrx_agent.cli "$@"\n' > /usr/local/bin/mtx \
    && chmod +x /usr/local/bin/mtx

# ─── ``browse`` CLI — the sandbox browser from any shell ─────────────────────
# `browse` is the platform's browser command: a thin front end on the SAME
# canonical Browser Manager path the Browser* tools use (see
# sdk/matrx_agent/cli/browse.py), never a second browser implementation.
# Preinstalled because the 2026-09-18 field report found an agent typing
# `browse` in a box and getting `command not found` (P2-2).
RUN printf '#!/bin/sh\nexec /usr/bin/python3 -m matrx_agent.cli browse "$@"\n' > /usr/local/bin/browse \
    && chmod +x /usr/local/bin/browse

# ─── Environment variables (defaults, overridden at runtime) ──────────────────
ENV SANDBOX_ID=""
ENV USER_ID=""
ENV S3_BUCKET=""
ENV S3_REGION="us-east-1"
ENV HOT_PATH="/home/agent"
ENV COLD_PATH="/data/cold"
ENV SHUTDOWN_TIMEOUT_SECONDS="30"

# ─── Expose SSH port ────────────────────────────────────────────────────────
EXPOSE 22

# ─── Health check ────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD /opt/sandbox/scripts/healthcheck.sh

# Image-variant marker — read by entrypoint-local.sh to set SANDBOX_MODE
# accurately when a derived image (matrx-sandbox:aidream, :local) reuses
# this entrypoint chain. Without it the env var said "local" even on
# :aidream containers.
RUN echo "core" > /etc/sandbox-image-tag

# Persist the build-time version into the box so it can self-verify on wake
# (the Phase-2 migrate/resume gate reads this) and so `mtx` / shells can show it.
# Keep the changing stamp after every dependency and source layer. Putting the
# ENV/LABEL near the top invalidated the full Playwright + apt build cache on
# every commit even when the image inputs were unchanged.
ARG MATRX_IMAGE_VERSION=dev
ENV MATRX_IMAGE_VERSION=${MATRX_IMAGE_VERSION}
LABEL com.aimatrx.sandbox.version="${MATRX_IMAGE_VERSION}"
RUN echo "${MATRX_IMAGE_VERSION}" > /etc/sandbox-image-version
# The SDK tree carries the same stamp: the binding-time runtime refresh
# (orchestrator/sdk_refresh.py + matrx_agent/selfupdate.py) reads it to say what
# a box is upgrading FROM, and rewrites it when it installs a newer tree — so
# `mtx whoami` reports the SDK actually on disk, not the image's birth date.
RUN echo "${MATRX_IMAGE_VERSION}" > /opt/sandbox/sdk/VERSION

# Use tini as init to handle signals properly
ENTRYPOINT ["tini", "--"]
CMD ["/opt/sandbox/scripts/entrypoint.sh"]
