#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="/var/log/sandbox/shutdown.log"
exec > >(tee -a "$LOG_FILE") 2>&1

TIMEOUT="${SHUTDOWN_TIMEOUT_SECONDS:-30}"

echo "=========================================="
echo "  Matrx Sandbox Shutting Down"
echo "  Sandbox ID: ${SANDBOX_ID:-unknown}"
echo "  User ID:    ${USER_ID:-unknown}"
echo "  Time:       $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "  Timeout:    ${TIMEOUT}s"
echo "=========================================="

# Remove ready marker
rm -f /tmp/.sandbox_ready

# ─── Step 0: Persistence module — auto-stash + final manifest ────────────────
# Runs BEFORE hot-sync so the manifest + any auto-stash branches make it
# into the synced /home/agent payload. Tight 30s budget — never block
# shutdown longer than that.
echo "[0/4] Running persistence module (manifest + auto-stash)..."
if timeout 30 curl -sS -X POST -m 28 \
        -H 'Content-Type: application/json' \
        -d '{"graceful":true,"auto_stash":true,"push_remote":true}' \
        http://127.0.0.1:8000/internal/shutdown >/var/log/sandbox/persistence-shutdown.json 2>&1; then
    echo "[0/4] Persistence shutdown OK — see /var/log/sandbox/persistence-shutdown.json"
else
    echo "[0/4] WARNING: persistence shutdown call failed or timed out (continuing anyway)"
fi

# ─── Step 1: Sync hot storage back to S3 ─────────────────────────────────────
echo "[1/4] Syncing hot storage to S3..."
if timeout "$TIMEOUT" /opt/sandbox/scripts/hot-sync.sh up; then
    echo "[1/4] Hot storage sync complete."
else
    echo "[1/4] WARNING: Hot storage sync failed or timed out."
fi

# ─── Step 2: Unmount cold storage ─────────────────────────────────────────────
echo "[2/4] Unmounting cold storage..."
/opt/sandbox/scripts/cold-mount.sh unmount
echo "[2/4] Cold storage unmounted."

# ─── Step 3: Cleanup ─────────────────────────────────────────────────────────
echo "[3/4] Cleaning up..."
# Remove temporary files
rm -rf /tmp/s3cache 2>/dev/null || true

echo "=========================================="
echo "  Shutdown complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

exit 0
