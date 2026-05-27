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

# Ask AI Dream whether the bridge is actually live. The probe endpoint is
# public (no auth) and returns {"configured": false} when the AI-Dream-side
# AIDREAM_SANDBOX_SERVICE_TOKEN is unset.
#
# Best-effort: if the probe is itself unreachable or returns a non-2xx (e.g.
# the deployed AI Dream predates the probe endpoint), we DON'T skip — we try
# the sync anyway and let mtx surface the real failure. We only short-circuit
# when the probe positively reports configured=false, since that's the one
# case where the env vars are present on this side but the bridge is known
# disabled on AI Dream's side and any sync attempt is guaranteed to 401.
PROBE_URL="${MATRX_AIDREAM_URL%/}/api/cloud-files/integrations.aidream"
PROBE_BODY="$(curl -fsS --max-time 5 "$PROBE_URL" 2>/dev/null || true)"
if [ -n "$PROBE_BODY" ] && echo "$PROBE_BODY" | grep -q '"configured":[[:space:]]*false'; then
    echo "[cloud-files-sync] AI Dream bridge reports configured=false, skipping ${DIRECTION}"
    exit 0
fi

mkdir -p "$CLOUD_DIR"

case "$DIRECTION" in
    down)
        # MATRX_CLOUD_FILES_LAZY=1 skips the eager boot copy — same-region S3
        # makes "first read pulls bytes on demand via mtx files get" cheap
        # enough to be the default in production. Combined with the
        # in-process Realtime / polling subscriber (CloudFilesWatcher), the
        # sandbox stays current with web-UI edits without paying a
        # potentially-multi-GB boot tax.
        # Default is still eager so existing deployments don't change shape
        # without an opt-in.
        if [ "${MATRX_CLOUD_FILES_LAZY:-0}" = "1" ]; then
            echo "[cloud-files-sync] MATRX_CLOUD_FILES_LAZY=1 — skipping eager down-sync; watcher handles freshness."
            # Still seed the down-complete marker so the upstream watcher
            # knows it can start observing the (empty) dir without panicking.
            mkdir -p "${HOME:-/home/agent}/.matrx/runtime"
            touch "${HOME:-/home/agent}/.matrx/runtime/cloud-files-down-complete"
            exit 0
        fi
        echo "[cloud-files-sync] Pulling user's cld_files → $CLOUD_DIR"
        # Tight 60s budget so a flaky AI Dream doesn't block sandbox startup.
        timeout 60 /usr/bin/python3.11 -m matrx_agent.cli files sync down \
            --dest "$CLOUD_DIR" || {
            echo "[cloud-files-sync] WARNING: down-sync failed or timed out"
            exit 0  # don't block startup
        }
        # Signal the in-process watcher (CloudFilesWatcher in matrx_agent.cloud_sync)
        # that the down-sync completed and it is safe to start observing without
        # re-uploading the bytes we just pulled. The watcher seeds its hash cache
        # from the current contents of $CLOUD_DIR before it begins.
        mkdir -p "${HOME:-/home/agent}/.matrx/runtime"
        touch "${HOME:-/home/agent}/.matrx/runtime/cloud-files-down-complete"
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
