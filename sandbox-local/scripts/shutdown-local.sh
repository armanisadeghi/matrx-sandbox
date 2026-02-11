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

# Stop ttyd
echo "[1/2] Stopping ttyd..."
pkill ttyd 2>/dev/null || true
echo "[1/2] ttyd stopped."

# Cleanup
echo "[2/2] Cleaning up..."
rm -rf /tmp/s3cache 2>/dev/null || true

echo "=========================================="
echo "  Shutdown complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

exit 0
