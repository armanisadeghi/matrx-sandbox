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

HOT_PATH="${HOT_PATH:-/home/agent}"

# ─── Step 1: Agent environment ───────────────────────────────────────────────
echo "[1/4] Preparing agent environment..."
chown -R agent:agent "$HOT_PATH"

mkdir -p /home/agent/.ssh
cp /opt/sandbox/config/admin_authorized_keys /home/agent/.ssh/authorized_keys
chown -R agent:agent /home/agent/.ssh
chmod 700 /home/agent/.ssh
chmod 600 /home/agent/.ssh/authorized_keys

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

# ─── Step 2: Start SSH server (optional human shell-in) ──────────────────────
echo "[2/4] Starting SSH server..."
/usr/sbin/sshd
echo "[2/4] SSH server running on port 22."

# ─── Step 3: Start Agent API Daemon (the capability surface) ─────────────────
echo "[3/4] Starting Sandbox API Daemon..."
sudo -E -u agent bash -c "cd /home/agent && python3 -m uvicorn matrx_agent.api.main:app --host 0.0.0.0 --port 8000 > /var/log/sandbox/api.log 2>&1 &"
echo "[3/4] Sandbox API Daemon running on port 8000."

# ─── Step 3.5: Pull AI Dream cloud_files (best effort; PDF/image use case) ───
echo "[3.5/4] Syncing AI Dream cloud_files (if configured)..."
sudo -E -u agent /opt/sandbox/scripts/cloud-files-sync.sh down || true
echo "[3.5/4] cloud_files sync complete."

# ─── Step 4: Signal readiness ─────────────────────────────────────────────────
echo "[4/4] Lightweight sandbox is READY."
touch /tmp/.sandbox_ready

# ─── Register shutdown handler ────────────────────────────────────────────────
trap '/opt/sandbox/scripts/shutdown-slim.sh' SIGTERM SIGINT

echo "Sandbox running. Waiting for agent commands or shutdown signal..."
while true; do
    sleep 10 &
    wait $!
done
