#!/usr/bin/env bash
set -euo pipefail

# Lightweight coding-box shutdown. See docs/EC2_LIGHTWEIGHT_BOX.md.
#
# The slim box has no S3 hot-sync and no FUSE cold mount to flush, so teardown
# is fast. But it KEEPS the persistence module's final pass — that's the
# "triple-check we didn't miss anything" safety net: it auto-stashes any dirty
# repos and pushes the stash branch to the remote, so uncommitted agent work
# isn't silently lost when an ephemeral box is reaped. Best-effort, tight budget.

LOG_FILE="/var/log/sandbox/shutdown.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "  Matrx Lightweight Sandbox Shutting Down (slim)"
echo "  Sandbox ID: ${SANDBOX_ID:-unknown}"
echo "  User ID:    ${USER_ID:-unknown}"
echo "  Time:       $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

rm -f /tmp/.sandbox_ready

# ─── Step 0: Persistence module — auto-stash dirty repos + push to remote ────
# This is the whole point of an ephemeral git box: don't lose work. The
# matrx_agent daemon scans /home/agent for dirty repos, stashes, and pushes
# the stash branch. push_remote=true because the remote IS our persistence.
echo "[0/2] Running persistence module (auto-stash + push)..."
if timeout 30 curl -sS -X POST -m 28 \
        -H 'Content-Type: application/json' \
        -d '{"graceful":true,"auto_stash":true,"push_remote":true}' \
        http://127.0.0.1:8000/internal/shutdown >/var/log/sandbox/persistence-shutdown.json 2>&1; then
    echo "[0/2] Persistence shutdown OK — see /var/log/sandbox/persistence-shutdown.json"
else
    echo "[0/2] WARNING: persistence shutdown call failed or timed out (continuing anyway)"
fi

# ─── Step 0.5: Push AI Dream cloud_files changes back (best effort) ──────────
echo "[0.5/2] Pushing cloud_files changes (if configured)..."
sudo -E -u agent /opt/sandbox/scripts/cloud-files-sync.sh up || true

# ─── Step 1: Cleanup ─────────────────────────────────────────────────────────
echo "[1/2] Cleaning up..."
rm -rf /tmp/s3cache 2>/dev/null || true

echo "=========================================="
echo "  Shutdown complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

exit 0
