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

# ─── Step 1: Sync hot storage back to S3 ─────────────────────────────────────
echo "[1/3] Syncing hot storage to S3..."
if timeout "$TIMEOUT" /opt/sandbox/scripts/hot-sync.sh up; then
    echo "[1/3] Hot storage sync complete."
else
    echo "[1/3] WARNING: Hot storage sync failed or timed out."
fi

# ─── Step 2: Unmount cold storage ─────────────────────────────────────────────
echo "[2/3] Unmounting cold storage..."
/opt/sandbox/scripts/cold-mount.sh unmount
echo "[2/3] Cold storage unmounted."

# ─── Step 3: Cleanup ─────────────────────────────────────────────────────────
echo "[3/3] Cleaning up..."
# Remove temporary files
rm -rf /tmp/s3cache 2>/dev/null || true

echo "=========================================="
echo "  Shutdown complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

exit 0
