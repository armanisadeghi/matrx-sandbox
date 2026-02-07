#!/usr/bin/env bash
# Health check script — returns 0 if sandbox is healthy, 1 otherwise

# Check 1: Sandbox has finished starting up
if [ ! -f /tmp/.sandbox_ready ]; then
    echo "NOT READY: sandbox still starting"
    exit 1
fi

# Check 2: Cold storage is mounted (if expected)
if [ -n "${COLD_PATH:-}" ] && [ -d "$COLD_PATH" ]; then
    if ! mountpoint -q "$COLD_PATH" 2>/dev/null; then
        echo "UNHEALTHY: cold storage not mounted"
        exit 1
    fi
fi

# Check 3: Agent home directory is accessible
if [ ! -d "${HOT_PATH:-/home/agent}" ]; then
    echo "UNHEALTHY: hot storage path missing"
    exit 1
fi

echo "HEALTHY"
exit 0
