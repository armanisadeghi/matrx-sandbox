#!/usr/bin/env bash
# Health check script — returns 0 if sandbox is healthy, 1 otherwise.
#
# Tier-aware: EC2 tier expects /data/cold to be a FUSE mountpoint (S3 backed).
# Hosted tier uses per-user Docker volumes and never mounts /data/cold —
# previously the cold-storage check failed every healthcheck for every hosted
# sandbox forever (~3 weeks of "unhealthy" in production before we caught it).
#
# Skip the FUSE check whenever:
#   - MATRX_TIER=hosted (orchestrator hosted tier), OR
#   - S3_BUCKET is empty (no S3 backend configured), OR
#   - matrx_agent's API on :8000 is up (the daemon being responsive is a
#     better signal of overall liveness than a mount check).

# Check 1: Sandbox has finished starting up
if [ ! -f /tmp/.sandbox_ready ]; then
    echo "NOT READY: sandbox still starting"
    exit 1
fi

# Check 2: matrx_agent API daemon is responsive. This is the new primary
# liveness signal — if the daemon answers on :8000, the sandbox is usable
# regardless of cold-storage state.
if curl -sf --max-time 3 -o /dev/null http://127.0.0.1:8000/health 2>/dev/null; then
    echo "HEALTHY"
    exit 0
fi

# Check 3: Cold storage mountpoint check — only when we genuinely expect
# the FUSE mount (EC2 tier with S3_BUCKET configured).
if [ "${MATRX_TIER:-}" != "hosted" ] && [ -n "${S3_BUCKET:-}" ] \
   && [ -n "${COLD_PATH:-}" ] && [ -d "$COLD_PATH" ]; then
    if ! mountpoint -q "$COLD_PATH" 2>/dev/null; then
        echo "UNHEALTHY: cold storage not mounted (EC2 tier; expected mountpoint at $COLD_PATH)"
        exit 1
    fi
fi

# Check 4: Agent home directory is accessible
if [ ! -d "${HOT_PATH:-/home/agent}" ]; then
    echo "UNHEALTHY: hot storage path missing"
    exit 1
fi

echo "HEALTHY"
exit 0
