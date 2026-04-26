#!/usr/bin/env bash
# Sync the user's AI Dream cld_files into ~/cloud-files/ (and back).
#
# This is how a sandbox sees files the user uploaded via the AI Dream UI.
# AI agents working inside the sandbox can `cat`, `grep`, `find`, etc. against
# these files using basic shell tools — no special API calls needed.
#
# Two modes:
#   down: sync FROM AI Dream cld_files INTO ~/cloud-files/ (called at startup)
#   up:   sync FROM ~/cloud-files/ INTO AI Dream cld_files (called at shutdown)
#
# Skips quietly if AI Dream env vars aren't set. The same image runs in
# environments where AI Dream isn't configured (purely-local dev, EC2 without
# AI Dream wired in yet, etc.).
#
# All real work is delegated to the ``mtx files`` Python CLI which lives at
# /opt/sandbox/sdk/matrx_agent/cli/__main__.py and knows how to talk to the
# cloud_sync REST surface.

set -euo pipefail

DIRECTION="${1:-down}"
CLOUD_DIR="${HOME:-/home/agent}/cloud-files"

# Required for the bridge — quiet skip if absent so the rest of startup
# isn't blocked.
if [ -z "${MATRX_AIDREAM_URL:-}" ] || [ -z "${MATRX_AIDREAM_SERVICE_TOKEN:-}" ]; then
    echo "[cloud-files-sync] AI Dream not configured, skipping ${DIRECTION} sync"
    exit 0
fi

if [ -z "${USER_ID:-}" ]; then
    echo "[cloud-files-sync] USER_ID not set, skipping"
    exit 0
fi

mkdir -p "$CLOUD_DIR"

case "$DIRECTION" in
    down)
        echo "[cloud-files-sync] Pulling user's cld_files → $CLOUD_DIR"
        # Tight 60s budget so a flaky AI Dream doesn't block sandbox startup.
        timeout 60 /usr/bin/python3.11 -m matrx_agent.cli files sync down \
            --dest "$CLOUD_DIR" || {
            echo "[cloud-files-sync] WARNING: down-sync failed or timed out"
            exit 0  # don't block startup
        }
        ;;
    up)
        echo "[cloud-files-sync] Pushing ~/cloud-files/ → cld_files"
        timeout 60 /usr/bin/python3.11 -m matrx_agent.cli files sync up \
            --src "$CLOUD_DIR" || {
            echo "[cloud-files-sync] WARNING: up-sync failed or timed out"
            exit 0
        }
        ;;
    *)
        echo "[cloud-files-sync] usage: $0 down|up" >&2
        exit 2
        ;;
esac
