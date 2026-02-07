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
    && rm -rf /var/lib/apt/lists/*

# ─── ripgrep and fd-find ─────────────────────────────────────────────────────
RUN curl -LO https://github.com/BurntSushi/ripgrep/releases/download/14.1.0/ripgrep_14.1.0-1_amd64.deb \
    && dpkg -i ripgrep_14.1.0-1_amd64.deb \
    && rm ripgrep_14.1.0-1_amd64.deb

RUN curl -LO https://github.com/sharkdp/fd/releases/download/v10.1.0/fd_10.1.0_amd64.deb \
    && dpkg -i fd_10.1.0_amd64.deb \
    && rm fd_10.1.0_amd64.deb

# ─── Python 3.11 ─────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# ─── Node.js 20 LTS ──────────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g npm@latest

# ─── AWS CLI v2 ──────────────────────────────────────────────────────────────
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip aws/

# ─── Mountpoint for Amazon S3 (cold storage FUSE) ────────────────────────────
RUN curl -LO https://s3.amazonaws.com/mountpoint-s3-release/latest/x86_64/mount-s3.deb \
    && dpkg -i mount-s3.deb \
    && rm mount-s3.deb

# ─── Chromium (headless browser for agent web access) ─────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium-browser \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Install playwright for Python (uses system Chromium)
RUN python3 -m pip install --no-cache-dir playwright \
    && playwright install-deps

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

# Allow agent to use FUSE
RUN echo "user_allow_other" >> /etc/fuse.conf

# ─── Copy sandbox scripts ────────────────────────────────────────────────────
COPY scripts/ /opt/sandbox/scripts/
RUN chmod +x /opt/sandbox/scripts/*.sh

# ─── Copy sandbox config ─────────────────────────────────────────────────────
COPY config/ /opt/sandbox/config/

# ─── Install Matrx agent SDK ─────────────────────────────────────────────────
COPY sdk/ /opt/sandbox/sdk/
RUN cd /opt/sandbox/sdk && python3 -m pip install --no-cache-dir -e .

# ─── Environment variables (defaults, overridden at runtime) ──────────────────
ENV SANDBOX_ID=""
ENV USER_ID=""
ENV S3_BUCKET=""
ENV S3_REGION="us-east-1"
ENV HOT_PATH="/home/agent"
ENV COLD_PATH="/data/cold"
ENV SHUTDOWN_TIMEOUT_SECONDS="30"

# ─── Health check ────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD /opt/sandbox/scripts/healthcheck.sh

# Use tini as init to handle signals properly
ENTRYPOINT ["tini", "--"]
CMD ["/opt/sandbox/scripts/entrypoint.sh"]
