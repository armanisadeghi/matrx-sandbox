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

MATRX_MIGRATION_COMMIT_MARKER="/tmp/.matrx-migration-committed"
MATRX_MIGRATION_ACTIVATED_MARKER="/tmp/.matrx-migration-activated"
AGENT_API_STARTED=0

start_agent_api() {
    echo "[hold] Starting Sandbox API Daemon..."
    # Held migrations must not leave Python bytecode in a mounted home before
    # the runtime's durable CAS has committed the replacement.
    PYTHONDONTWRITEBYTECODE=1 sudo -E -u agent bash -c "cd /home/agent && PYTHONDONTWRITEBYTECODE=1 python3 -m uvicorn matrx_agent.api.main:app --host 0.0.0.0 --port 8000 > /var/log/sandbox/api.log 2>&1 &"
    AGENT_API_STARTED=1
}

if [ "${MATRX_MIGRATION_HOLD:-}" = "1" ] && [ ! -f "$MATRX_MIGRATION_COMMIT_MARKER" ]; then
    rm -f "$MATRX_MIGRATION_ACTIVATED_MARKER"
    export PYTHONDONTWRITEBYTECODE=1
    echo "Migration hold active: API health only; home boot is deferred until commit marker."
    start_agent_api
    touch /tmp/.sandbox_ready
    while [ ! -f "$MATRX_MIGRATION_COMMIT_MARKER" ]; do sleep 1; done
    echo "Migration commit marker observed; activating normal boot."
fi

# ─── Step 1: Sync hot storage from S3 ────────────────────────────────────────
if [ "${SANDBOX_MIGRATION:-}" = "1" ]; then
    echo "[1/5] Migration boot — skipping hot storage download."
else
    echo "[1/5] Syncing hot storage from S3..."
    /opt/sandbox/scripts/hot-sync.sh down
    echo "[1/5] Hot storage sync complete."
fi

# ─── Step 2: Mount cold storage via FUSE ──────────────────────────────────────
echo "[2/5] Mounting cold storage FUSE filesystem..."
/opt/sandbox/scripts/cold-mount.sh mount
echo "[2/5] Cold storage mounted."

# ─── Step 3: Set up environment for agent ─────────────────────────────────────
echo "[3/5] Preparing agent environment..."

/opt/sandbox/scripts/prepare-agent-home.sh

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

# ─── Step 3.5: Ensure canonical /home/agent layout ───────────────────────────
# Idempotent — creates .matrx/{plans,skills,instructions,memory,runtime/...},
# cloud-files/, repos/, projects/, scratch/, and writes the agent-facing
# SANDBOX_LAYOUT.md so the agent has a single source of truth for paths.
echo "[3.5/5] Ensuring canonical sandbox layout..."
/opt/sandbox/scripts/ensure-layout.sh
echo "[3.5/5] Layout ready."

# ─── Step 3.6: Configure git credentials ────────────────────────────────────
# User secrets are injected into the container env by the orchestrator. Git
# does not read GITHUB_PAT/GH_TOKEN by itself, so install the standard helper
# config for both env-backed tokens and POST /credentials cache tokens.
echo "[3.6/5] Configuring git credential helpers..."
sudo -H -E -u agent /opt/sandbox/scripts/configure-git-credentials.sh || true
echo "[3.6/5] Git credential helpers ready."

# ─── Step 4: Start SSH server ────────────────────────────────────────────────
echo "[4/5] Starting SSH server..."
/usr/sbin/sshd
echo "[4/5] SSH server running on port 22."

# ─── Step 4.5: Start Agent API Daemon ────────────────────────────────────────
echo "[4.5/5] Starting Sandbox API Daemon..."
# ``-E`` so env vars (SANDBOX_ID, USER_ID, MATRX_TIER, etc.) reach the
# persistence module — see comment in entrypoint-local.sh for why.
if [ "$AGENT_API_STARTED" = "0" ]; then
    start_agent_api
fi
echo "[4.5/5] Sandbox API Daemon running on port 8000."

# ─── Step 4.6: Pull AI Dream cloud_files into ~/cloud-files/ ─────────────────
# Bridges the user's AI Dream uploads into the sandbox filesystem so agents
# can use `cat`, `grep`, `find`, etc. natively. No-op if AI Dream env vars
# aren't set.
if [ "${SANDBOX_MIGRATION:-}" = "1" ]; then
    # Zero-drift migration boot: the per-user volume already holds the data
    # (we're swapping the image under the SAME volume), so re-pulling cloud_files
    # is wasteful and is the single biggest contributor to migration time. Skip.
    echo "[4.6/5] Migration boot — skipping cloud_files down-sync (data already on the volume)."
else
    echo "[4.6/5] Syncing AI Dream cloud_files (if configured)..."
    sudo -E -u agent /opt/sandbox/scripts/cloud-files-sync.sh down || true
    echo "[4.6/5] cloud_files sync complete."
fi

# ─── Step 5: Signal readiness ────────────────────────────────────────────────
echo "[5/5] Sandbox is READY."
touch /tmp/.sandbox_ready
if [ "${MATRX_MIGRATION_HOLD:-}" = "1" ]; then
    touch "$MATRX_MIGRATION_ACTIVATED_MARKER"
fi

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
