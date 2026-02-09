#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# test-shell.sh — Bootstrap a test-friendly shell inside the sandbox container
#
# Use this instead of the normal entrypoint for manual testing.
# It skips S3 sync, FUSE mounts, and all orchestrator dependencies.
#
# How to use:
#   docker run -it --rm matrx-sandbox /opt/sandbox/scripts/test-shell.sh
#
# Or build & run in one shot:
#   docker build -t matrx-sandbox sandbox-image/
#   docker run -it --rm matrx-sandbox /opt/sandbox/scripts/test-shell.sh
#
# What it sets up:
#   - Switches to the 'agent' user (same as production)
#   - Sets HOT_PATH, CWD, and other env vars
#   - Drops you into an interactive bash shell at /home/agent
#   - The smoke test is available at: smoke-test
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║           matrx-sandbox — Test Shell                        ║"
echo "║                                                             ║"
echo "║  You are running as: $(whoami) (uid=$(id -u))                         ║"
echo "║  Python:  $(python3 --version 2>&1)                           ║"
echo "║  Node:    $(node --version 2>&1)                              ║"
echo "║                                                             ║"
echo "║  Quick commands:                                            ║"
echo "║    smoke-test          Run the full tool smoke test         ║"
echo "║    python3 -c 'from matrx_tools import ...'  Test imports   ║"
echo "║                                                             ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo ""

# Set up environment like the real entrypoint would
export HOT_PATH="${HOT_PATH:-/home/agent}"
export COLD_PATH="${COLD_PATH:-/data/cold}"
export SANDBOX_ID="${SANDBOX_ID:-test-local}"
export USER_ID="${USER_ID:-test-user}"
export HOME="/home/agent"

# Create an alias for the smoke test
cat >> /home/agent/.bashrc << 'EOF'
alias smoke-test='bash /opt/sandbox/scripts/smoke-test.sh'
export PS1='\[\033[0;36m\]sandbox\[\033[0m\]:\[\033[0;33m\]\w\[\033[0m\]\$ '
EOF

# Drop into interactive shell as agent user (or root if already root)
if [ "$(id -u)" -eq 0 ]; then
    exec su - agent -c "cd /home/agent && exec bash --login"
else
    cd /home/agent
    exec bash --login
fi
