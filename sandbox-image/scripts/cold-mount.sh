#!/usr/bin/env bash
set -euo pipefail

# Cold storage FUSE mount/unmount script
# Uses AWS Mountpoint for S3 (mount-s3)
# Usage: cold-mount.sh [mount|unmount]

ACTION="${1:-}"
S3_COLD_PREFIX="users/${USER_ID}/cold"
LOCAL_COLD_PATH="${COLD_PATH:-/data/cold}"
LOG_FILE="/var/log/sandbox/cold-mount.log"

if [ -z "$ACTION" ]; then
    echo "Usage: cold-mount.sh [mount|unmount]" >&2
    exit 1
fi

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] COLD-MOUNT[$ACTION]: $*" | tee -a "$LOG_FILE"
}

case "$ACTION" in
    mount)
        log "Mounting cold storage: s3://${S3_BUCKET}/${S3_COLD_PREFIX} → $LOCAL_COLD_PATH"
        mkdir -p "$LOCAL_COLD_PATH"

        # Mount using AWS Mountpoint for S3
        # --prefix scopes to the user's cold directory
        # --allow-other lets the agent user access the mount
        # --cache /tmp/s3cache enables local caching for repeated reads
        # --metadata-ttl 60 caches directory listings for 60 seconds
        mount-s3 "$S3_BUCKET" "$LOCAL_COLD_PATH" \
            --prefix "$S3_COLD_PREFIX/" \
            --region "${S3_REGION:-us-east-1}" \
            --allow-other \
            --cache /tmp/s3cache \
            --metadata-ttl 60 \
            --log-directory /var/log/sandbox \
            --log-metrics

        # Verify the mount
        if mountpoint -q "$LOCAL_COLD_PATH"; then
            log "Cold storage mounted successfully"
        else
            log "ERROR: Cold storage mount verification failed"
            exit 1
        fi
        ;;

    unmount)
        log "Unmounting cold storage at $LOCAL_COLD_PATH"

        if mountpoint -q "$LOCAL_COLD_PATH" 2>/dev/null; then
            # Flush any pending writes
            sync

            # Lazy unmount to handle busy filesystems gracefully
            umount -l "$LOCAL_COLD_PATH" 2>/dev/null || true
            log "Cold storage unmounted"
        else
            log "Cold storage was not mounted, nothing to unmount"
        fi
        ;;

    *)
        echo "Unknown action: $ACTION. Use 'mount' or 'unmount'." >&2
        exit 1
        ;;
esac
