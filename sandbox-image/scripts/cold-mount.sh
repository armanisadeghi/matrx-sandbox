#!/usr/bin/env bash
set -euo pipefail

# Cold storage FUSE mount/unmount script
# Uses AWS Mountpoint for S3 (mount-s3)
# Usage: cold-mount.sh [mount|unmount]

ACTION="${1:-}"

# Validate required environment variables
if [ -z "${S3_BUCKET:-}" ] || [ -z "${USER_ID:-}" ]; then
    echo "ERROR: S3_BUCKET and USER_ID must be set" >&2
    exit 1
fi

S3_COLD_PREFIX="users/${USER_ID}/cold"
LOCAL_COLD_PATH="${COLD_PATH:-/data/cold}"
# Overridable so the mount/adopt/recover decision is reachable from a test —
# the log path is /var/log/sandbox in a real box and nowhere else.
LOG_FILE="${COLD_MOUNT_LOG_FILE:-/var/log/sandbox/cold-mount.log}"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

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
        mkdir -p /tmp/s3cache

        # IDEMPOTENCY (feedback 81cb4265, 2026-09-18). `mount-s3` exits non-zero
        # with "mount point /data/cold is already mounted", and under `set -e`
        # that killed the whole boot at step [2/5]. A container that died without
        # running shutdown.sh (SIGKILL, OOM, a crash at any later boot step)
        # leaves the FUSE mount behind on the user's volume, so ONE failed start
        # made every later start for that user fail too — the box read `failed`
        # with the real cause buried in the docker log.
        #
        # A mount we find is one of two things, and we say which:
        #   serving  → adopt it; re-mounting the same bucket gains nothing.
        #   dead     → a FUSE endpoint whose mount-s3 process is gone. Listing it
        #              fails with ENOTCONN, so lazily unmount and mount fresh.
        if mountpoint -q "$LOCAL_COLD_PATH" 2>/dev/null; then
            if timeout 10 ls -1 "$LOCAL_COLD_PATH" >/dev/null 2>&1; then
                log "Cold storage already mounted at $LOCAL_COLD_PATH and serving — adopting it"
                exit 0
            fi
            log "WARNING: $LOCAL_COLD_PATH is mounted but not serving (stale FUSE endpoint from a container that died without shutdown) — unmounting it before remounting"
            umount -l "$LOCAL_COLD_PATH" 2>/dev/null || umount -f "$LOCAL_COLD_PATH" 2>/dev/null || true
            if mountpoint -q "$LOCAL_COLD_PATH" 2>/dev/null; then
                log "ERROR: could not clear the stale mount at $LOCAL_COLD_PATH"
                log "REMEDY: the container needs SYS_ADMIN and /dev/fuse to unmount; stop the sandbox and start it again to get a fresh mount namespace."
                exit 1
            fi
            log "Stale mount cleared"
        fi

        # Mount using AWS Mountpoint for S3
        # --prefix scopes to the user's cold directory
        # --allow-other lets the agent user access the mount
        # --cache /tmp/s3cache enables local caching for repeated reads
        # --metadata-ttl 60 caches directory listings for 60 seconds
        timeout 30 mount-s3 "$S3_BUCKET" "$LOCAL_COLD_PATH" \
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
            log "REMEDY: mount-s3 returned 0 but $LOCAL_COLD_PATH is not a mount point — check /var/log/sandbox for the mount-s3 log and that the container has /dev/fuse."
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
