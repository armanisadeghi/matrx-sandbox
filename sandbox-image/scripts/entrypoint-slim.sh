#!/usr/bin/env bash
set -euo pipefail

# Lightweight coding-box entrypoint. See docs/EC2_LIGHTWEIGHT_BOX.md.
#
# Difference from the :core entrypoint.sh — this box's persistence is GIT, not
# S3, so the two slowest boot steps are GONE:
#   - NO hot-sync from S3   (the 5–30s wait that dominates :core cold start)
#   - NO cold-mount FUSE
# What remains is fast: agent env + canonical layout + sshd + the matrx_agent
# daemon. cloud_files sync stays as a best-effort no-op (only fires if the AI
# Dream env vars are present — useful for the PDF/image "files copied in" case).

LOG_FILE="/var/log/sandbox/entrypoint.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "  Matrx Lightweight Sandbox Starting (slim)"
echo "  Sandbox ID: ${SANDBOX_ID:-unknown}"
echo "  User ID:    ${USER_ID:-unknown}"
echo "  Time:       $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

# ─── Validate required env vars ──────────────────────────────────────────────
# NOTE: S3_BUCKET is intentionally NOT required here — the slim box never
# touches S3. Only identity vars matter.
for var in SANDBOX_ID USER_ID; do
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

HOT_PATH="${HOT_PATH:-/home/agent}"

# ─── Step 1: Agent environment ───────────────────────────────────────────────
echo "[1/4] Preparing agent environment..."
/opt/sandbox/scripts/prepare-agent-home.sh

cat > /home/agent/.sandbox_env <<EOF
export SANDBOX_ID="${SANDBOX_ID}"
export USER_ID="${USER_ID}"
export HOT_PATH="${HOT_PATH}"
EOF
chown agent:agent /home/agent/.sandbox_env
if ! grep -q '.sandbox_env' /home/agent/.bashrc 2>/dev/null; then
    echo '[ -f ~/.sandbox_env ] && source ~/.sandbox_env' >> /home/agent/.bashrc
fi
echo "[1/4] Agent environment ready."

# ─── Step 1.5: Ensure canonical /home/agent layout ───────────────────────────
echo "[1.5/4] Ensuring canonical sandbox layout..."
/opt/sandbox/scripts/ensure-layout.sh
echo "[1.5/4] Layout ready."

# ─── Step 1.6: Configure git credentials ────────────────────────────────────
# Slim boxes use git as the persistence/update path. Make injected GitHub
# tokens usable by plain `git pull` / `git push` without writing the token into
# the mounted home volume.
echo "[1.6/4] Configuring git credential helpers..."
sudo -H -E -u agent /opt/sandbox/scripts/configure-git-credentials.sh || true
echo "[1.6/4] Git credential helpers ready."

# ─── Step 2: Start SSH server (optional human shell-in) ──────────────────────
echo "[2/4] Starting SSH server..."
/usr/sbin/sshd
echo "[2/4] SSH server running on port 22."

# ─── Step 3: Start Agent API Daemon (the capability surface) ─────────────────
echo "[3/4] Starting Sandbox API Daemon..."
if [ "$AGENT_API_STARTED" = "0" ]; then
    start_agent_api
fi
echo "[3/4] Sandbox API Daemon running on port 8000."

# ─── Step 3.5: Pull AI Dream cloud_files (best effort; PDF/image use case) ───
if [ "${SANDBOX_MIGRATION:-}" = "1" ]; then
    # Zero-drift migration boot: data is already on the per-user volume we're
    # re-mounting, so skip the (up to 60s) cloud_files down-sync — the biggest
    # contributor to migration time.
    echo "[3.5/4] Migration boot — skipping cloud_files down-sync (data already on the volume)."
else
    echo "[3.5/4] Syncing AI Dream cloud_files (if configured)..."
    sudo -E -u agent /opt/sandbox/scripts/cloud-files-sync.sh down || true
    echo "[3.5/4] cloud_files sync complete."
fi

# ─── Step 4: Signal readiness ─────────────────────────────────────────────────
echo "[4/4] Lightweight sandbox is READY."
touch /tmp/.sandbox_ready
if [ "${MATRX_MIGRATION_HOLD:-}" = "1" ]; then
    touch "$MATRX_MIGRATION_ACTIVATED_MARKER"
fi

# ─── Register shutdown handler ────────────────────────────────────────────────
trap '/opt/sandbox/scripts/shutdown-slim.sh' SIGTERM SIGINT

echo "Sandbox running. Waiting for agent commands or shutdown signal..."
while true; do
    sleep 10 &
    wait $!
done
