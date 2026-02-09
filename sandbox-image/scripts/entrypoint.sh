#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="/var/log/sandbox/entrypoint.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "  Matrx Sandbox Starting"
echo "  Sandbox ID: ${SANDBOX_ID:-unknown}"
echo "  User ID:    ${USER_ID:-unknown}"
echo "  Time:       $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

# ─── Validate required env vars ──────────────────────────────────────────────
for var in SANDBOX_ID USER_ID S3_BUCKET; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: Required environment variable $var is not set"
        exit 1
    fi
done

# ─── Step 1: Sync hot storage from S3 ────────────────────────────────────────
echo "[1/5] Syncing hot storage from S3..."
/opt/sandbox/scripts/hot-sync.sh down
echo "[1/5] Hot storage sync complete."

# ─── Step 2: Mount cold storage via FUSE ──────────────────────────────────────
echo "[2/5] Mounting cold storage FUSE filesystem..."
/opt/sandbox/scripts/cold-mount.sh mount
echo "[2/5] Cold storage mounted."

# ─── Step 3: Set up environment for agent ─────────────────────────────────────
echo "[3/5] Preparing agent environment..."

# Ensure agent owns their home directory after hot sync
chown -R agent:agent "$HOT_PATH"

# Restore SSH authorized_keys (hot sync may have overwritten /home/agent)
mkdir -p /home/agent/.ssh
cp /opt/sandbox/config/admin_authorized_keys /home/agent/.ssh/authorized_keys
chown -R agent:agent /home/agent/.ssh
chmod 700 /home/agent/.ssh
chmod 600 /home/agent/.ssh/authorized_keys

# Write a small env file the agent can source
cat > /home/agent/.sandbox_env <<EOF
export SANDBOX_ID="${SANDBOX_ID}"
export USER_ID="${USER_ID}"
export HOT_PATH="${HOT_PATH}"
export COLD_PATH="${COLD_PATH}"
EOF
chown agent:agent /home/agent/.sandbox_env

# Source it in agent's bashrc if not already there
if ! grep -q '.sandbox_env' /home/agent/.bashrc 2>/dev/null; then
    echo '[ -f ~/.sandbox_env ] && source ~/.sandbox_env' >> /home/agent/.bashrc
fi

echo "[3/5] Agent environment ready."

# ─── Step 4: Start SSH server ────────────────────────────────────────────────
echo "[4/5] Starting SSH server..."
/usr/sbin/sshd
echo "[4/5] SSH server running on port 22."

# ─── Step 5: Signal readiness ────────────────────────────────────────────────
echo "[5/5] Sandbox is READY."
touch /tmp/.sandbox_ready

# ─── Register shutdown handler ────────────────────────────────────────────────
trap '/opt/sandbox/scripts/shutdown.sh' SIGTERM SIGINT

# ─── Keep container running ───────────────────────────────────────────────────
# In production, the orchestrator will exec commands into this container.
# For now, we sleep and wait for signals.
echo "Sandbox running. Waiting for agent commands or shutdown signal..."
while true; do
    sleep 10 &
    wait $!
done
