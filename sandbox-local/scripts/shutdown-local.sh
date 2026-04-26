#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="/var/log/sandbox/shutdown.log"
mkdir -p /var/log/sandbox
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "  Matrx Sandbox Shutting Down (Local)"
echo "  Sandbox ID: ${SANDBOX_ID:-unknown}"
echo "  Time:       $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

# Remove ready marker
rm -f /tmp/.sandbox_ready

# ─── Step 0: Persistence module — auto-stash + final manifest ────────────────
# Same hook as production EC2 shutdown.sh. Hits the in-container daemon
# at 127.0.0.1:8000 to flush a final session.json + auto-stash dirty repos.
echo "[0/3] Running persistence module..."
if timeout 30 curl -sS -X POST -m 28 \
        -H 'Content-Type: application/json' \
        -d '{"graceful":true,"auto_stash":true,"push_remote":true}' \
        http://127.0.0.1:8000/internal/shutdown >/var/log/sandbox/persistence-shutdown.json 2>&1; then
    echo "[0/3] Persistence shutdown OK"
else
    echo "[0/3] WARNING: persistence shutdown call failed or timed out (continuing)"
fi

# Stop the matrx_agent daemon AFTER the shutdown call so the final manifest
# write completes before uvicorn dies.
pkill -f 'uvicorn matrx_agent.api.main' 2>/dev/null || true

# Stop ttyd
echo "[1/3] Stopping ttyd..."
pkill ttyd 2>/dev/null || true
echo "[1/3] ttyd stopped."

# Cleanup
echo "[2/3] Cleaning up..."
rm -rf /tmp/s3cache 2>/dev/null || true

echo "=========================================="
echo "  Shutdown complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

exit 0
