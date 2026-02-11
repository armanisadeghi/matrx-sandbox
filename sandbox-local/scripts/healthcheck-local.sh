#!/usr/bin/env bash
# Health check for local-mode sandbox
# No FUSE/S3 checks — just readiness + ttyd + agent home

# Check 1: Sandbox has finished starting up
if [ ! -f /tmp/.sandbox_ready ]; then
    echo "NOT READY: sandbox still starting"
    exit 1
fi

# Check 2: Agent home directory is accessible
if [ ! -d "${HOT_PATH:-/home/agent}" ]; then
    echo "UNHEALTHY: agent home missing"
    exit 1
fi

# Check 3: ttyd is running (if expected)
if command -v ttyd &>/dev/null; then
    if ! pgrep -x ttyd >/dev/null 2>&1; then
        echo "UNHEALTHY: ttyd not running"
        exit 1
    fi
fi

echo "HEALTHY"
exit 0
