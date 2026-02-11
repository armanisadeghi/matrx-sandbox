#!/usr/bin/env bash
set -euo pipefail

mkdir -p /var/log/sandbox
LOG_FILE="/var/log/sandbox/entrypoint.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "  Matrx Sandbox Starting (Local Mode)"
echo "  Sandbox ID: ${SANDBOX_ID:-unknown}"
echo "  Time:       $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

# ─── Step 1: Skip S3 in local mode ──────────────────────────────────────────
echo "[1/5] S3 sync skipped (local mode — using Docker volumes)"

# ─── Step 2: Skip FUSE mount in local mode ──────────────────────────────────
echo "[2/5] FUSE mount skipped (local mode)"

# ─── Step 3: Set up environment for agent ────────────────────────────────────
echo "[3/5] Preparing agent environment..."

HOT_PATH="${HOT_PATH:-/home/agent}"

chown -R agent:agent "$HOT_PATH" 2>/dev/null || true

# Ensure standard dirs exist
mkdir -p /home/agent/.ssh /home/agent/workspace /data/cold
chown -R agent:agent /home/agent /data/cold

# Restore SSH keys if present in config
if [ -f /opt/sandbox/config/admin_authorized_keys ]; then
    cp /opt/sandbox/config/admin_authorized_keys /home/agent/.ssh/authorized_keys
    chown agent:agent /home/agent/.ssh/authorized_keys
    chmod 600 /home/agent/.ssh/authorized_keys
fi

# Write env file the agent can source
cat > /home/agent/.sandbox_env <<EOF
export SANDBOX_ID="${SANDBOX_ID:-sandbox}"
export HOT_PATH="${HOT_PATH}"
export SANDBOX_MODE="local"
export SANDBOX_NAME="${SANDBOX_NAME:-sandbox}"
EOF
chown agent:agent /home/agent/.sandbox_env

# Source it in agent's bashrc if not already there
if ! grep -q '.sandbox_env' /home/agent/.bashrc 2>/dev/null; then
    echo '[ -f ~/.sandbox_env ] && source ~/.sandbox_env' >> /home/agent/.bashrc
fi

# Set a nice prompt
if ! grep -q 'SANDBOX_PROMPT' /home/agent/.bashrc 2>/dev/null; then
    cat >> /home/agent/.bashrc <<'PROMPT_EOF'
# SANDBOX_PROMPT
export PS1="\[\033[1;36m\]sandbox:${SANDBOX_ID:-?}\[\033[0m\] \[\033[1;34m\]\w\[\033[0m\] \$ "
alias ll='ls -alh --color=auto'
alias la='ls -A --color=auto'
PROMPT_EOF
fi

echo "[3/5] Agent environment ready."

# ─── Step 4: Start SSH server ────────────────────────────────────────────────
echo "[4/5] Starting SSH server..."
mkdir -p /run/sshd
/usr/sbin/sshd 2>/dev/null || echo "  SSH start skipped (non-critical in local mode)"
echo "[4/5] SSH server started."

# ─── Step 4b: Start ttyd (web terminal) ─────────────────────────────────────
if command -v ttyd &>/dev/null; then
    TTYD_PORT="${TTYD_PORT:-7681}"
    echo "[4b] Starting ttyd web terminal on port ${TTYD_PORT}..."

    TTYD_ARGS=(
        --port "$TTYD_PORT"
        --writable
    )

    # Add credentials if provided
    if [ -n "${TTYD_USER:-}" ] && [ -n "${TTYD_PASS:-}" ]; then
        TTYD_ARGS+=(--credential "${TTYD_USER}:${TTYD_PASS}")
    fi

    # Run ttyd in background, login as agent user
    ttyd "${TTYD_ARGS[@]}" su - agent &>/var/log/sandbox/ttyd.log &
    TTYD_PID=$!
    echo "[4b] ttyd running (pid: ${TTYD_PID}) on :${TTYD_PORT}"
else
    echo "[4b] ttyd not installed — web terminal unavailable"
fi

# ─── Step 5: Signal readiness ────────────────────────────────────────────────
echo "[5/5] Sandbox is READY."
touch /tmp/.sandbox_ready

# ─── Shutdown handler ────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "=========================================="
    echo "  Sandbox shutting down"
    echo "  Sandbox ID: ${SANDBOX_ID:-unknown}"
    echo "  Time:       $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "=========================================="
    rm -f /tmp/.sandbox_ready
    pkill ttyd 2>/dev/null || true
    echo "Shutdown complete."
    exit 0
}
trap cleanup SIGTERM SIGINT

# ─── Keep container running ──────────────────────────────────────────────────
echo "Sandbox running. Waiting for commands or shutdown signal..."
while true; do
    sleep 10 &
    wait $!
done
