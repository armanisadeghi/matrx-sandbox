#!/usr/bin/env bash
set -euo pipefail

# Hot storage sync script
# Usage: hot-sync.sh [down|up]
#   down = S3 → local (startup)
#   up   = local → S3 (shutdown)

DIRECTION="${1:-}"

# Validate required environment variables
if [ -z "${S3_BUCKET:-}" ] || [ -z "${USER_ID:-}" ]; then
    echo "ERROR: S3_BUCKET and USER_ID must be set" >&2
    exit 1
fi

S3_HOT_PREFIX="s3://${S3_BUCKET}/users/${USER_ID}/hot/"
LOCAL_HOT_PATH="${HOT_PATH:-/home/agent}"
LOG_FILE="/var/log/sandbox/hot-sync.log"

if [ -z "$DIRECTION" ]; then
    echo "Usage: hot-sync.sh [down|up]" >&2
    exit 1
fi

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] HOT-SYNC[$DIRECTION]: $*" | tee -a "$LOG_FILE"
}

sync_with_retry() {
    local src="$1"
    local dst="$2"
    local max_retries=3
    local retry=0

    while [ $retry -lt $max_retries ]; do
        if aws s3 sync "$src" "$dst" \
            --no-progress \
            --exclude "*.tmp" \
            --exclude ".DS_Store" \
            --exclude "__pycache__/*"; then
            return 0
        fi
        retry=$((retry + 1))
        log "Sync attempt $retry failed, retrying in ${retry}s..."
        sleep "$retry"
    done

    log "ERROR: Sync failed after $max_retries attempts"
    return 1
}

case "$DIRECTION" in
    down)
        log "Starting hot sync DOWN: $S3_HOT_PREFIX → $LOCAL_HOT_PATH"
        mkdir -p "$LOCAL_HOT_PATH"

        # Check if the S3 prefix exists (user may be new with no files)
        if aws s3 ls "$S3_HOT_PREFIX" --region "${S3_REGION:-us-east-1}" > /dev/null 2>&1; then
            sync_with_retry "$S3_HOT_PREFIX" "$LOCAL_HOT_PATH"
            FILE_COUNT=$(find "$LOCAL_HOT_PATH" -type f | wc -l)
            log "Synced $FILE_COUNT files to local hot storage"
        else
            log "No existing hot storage found for user $USER_ID — starting fresh"
        fi
        ;;

    up)
        log "Starting hot sync UP: $LOCAL_HOT_PATH → $S3_HOT_PREFIX"

        if [ -d "$LOCAL_HOT_PATH" ]; then
            sync_with_retry "$LOCAL_HOT_PATH" "$S3_HOT_PREFIX"
            log "Hot storage synced back to S3"
        else
            log "WARNING: Hot path $LOCAL_HOT_PATH does not exist, nothing to sync"
        fi
        ;;

    *)
        echo "Unknown direction: $DIRECTION. Use 'down' or 'up'." >&2
        exit 1
        ;;
esac
