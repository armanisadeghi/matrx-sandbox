#!/usr/bin/env bash
# aidream-downstream-entrypoint.sh — WHICH downstream entrypoint an aidream box
# hands off to. Prints the absolute path on stdout, or exits non-zero with the
# reason on stderr.
#
# It is a separate file for one reason: the decision is testable and the boot
# script around it is not (that script seeds 2.7 GB before it gets here). The
# decision was wrong for five days without anything able to notice.
#
# THE TIER DECIDES, not the presence of an S3 variable. The old inline branch
# tested `S3_BUCKET` alone, on the premise that "the hosted tier doesn't set
# it". The hosted orchestrator DOES set it (`sandbox_manager.py` passes
# `S3_BUCKET: location.s3_bucket or ""` to every container), so every hosted
# aidream box took the production branch, ran the EC2 hot-sync/cold-mount path,
# and died on `mount point /data/cold is already mounted` with exit 1. The box
# never reached a usable state and the row read `failed` with the real reason
# buried four screens up in the docker log. Observed 2026-09-18 on
# sbx-9a6aeaba3be8; the last hosted aidream box that came up was 2026-09-13.
#
# `MATRX_TIER` is the orchestrator's own statement of where this box runs, and
# it is already the discriminator in `healthcheck.sh` and in
# `entrypoint-aidream.sh` above this hand-off. Two discriminators for one fact
# is what made the divergence possible.
set -uo pipefail

SCRIPT_DIR="${AIDREAM_SCRIPT_DIR:-/opt/sandbox/scripts}"
PRODUCTION="${SCRIPT_DIR}/entrypoint.sh"
LOCAL="${SCRIPT_DIR}/entrypoint-local.sh"

if [ "${MATRX_TIER:-}" = "hosted" ]; then
    if [ -x "$LOCAL" ]; then
        printf '%s\n' "$LOCAL"
        exit 0
    fi
    # Loud, never a silent fall-through to the S3 path: on the hosted tier that
    # path cannot work, and the failure it produces names a mount instead of
    # the missing script.
    echo "tier=hosted but ${LOCAL} is not present/executable in this image" >&2
    exit 1
fi

if [ -n "${S3_BUCKET:-}" ]; then
    if [ -x "$PRODUCTION" ]; then
        printf '%s\n' "$PRODUCTION"
        exit 0
    fi
    echo "S3_BUCKET is set but ${PRODUCTION} is not present/executable in this image" >&2
    exit 1
fi

if [ -x "$LOCAL" ]; then
    printf '%s\n' "$LOCAL"
    exit 0
fi

echo "no MATRX_TIER, no S3_BUCKET, and ${LOCAL} is not present in this image" >&2
exit 1
