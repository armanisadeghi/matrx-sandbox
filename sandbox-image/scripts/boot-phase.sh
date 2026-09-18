#!/usr/bin/env bash
# The box tells the host which phase of boot it is in — nobody guesses.
#
# WHY THIS EXISTS. The orchestrator used to wait a hardcoded 120 seconds for
# /tmp/.sandbox_ready and declare the box `failed` if it had not appeared.
# That number described a small test home, not the system: a user with ~8,600
# files in S3 spends longer than that in step [1/5] alone, so their box was
# killed mid-restore and every later create was refused because the dead row
# still held one of their five admission slots. A readiness signal that is a
# wall clock is a bug class; this script is the other half of its fix
# (orchestrator/orchestrator/boot_readiness.py is the first).
#
# Usage:
#   boot-phase.sh <phase> [done] [total]
#
# <phase> is one of: container home_sync cold_mount environment sdk
#                    cloud_files ready   (see boot_readiness.KNOWN_PHASES)
# [done]/[total] are a file count for phases that copy things, and become the
# "home sync in progress: N/M files" line the caller sees.
#
# Everything lives in /tmp — the CONTAINER layer, never the user's home
# volume, so nothing here can collide with THE HOME OWNERSHIP LAW or leak
# between boots of the same volume. A failure to publish must never take the
# box down: the orchestrator treats a silent box as an ordinary un-phased one.

BOOT_DIR="${MATRX_BOOT_DIR:-/tmp/matrx-boot}"
phase="${1:-}"
done_count="${2:-}"
total_count="${3:-}"

[ -n "$phase" ] || exit 0

{
    mkdir -p "$BOOT_DIR" || exit 0
    printf '%s\n' "$phase" > "$BOOT_DIR/phase"
    if [ -n "$done_count" ] || [ -n "$total_count" ]; then
        printf '%s/%s\n' "${done_count:-0}" "${total_count:-0}" > "$BOOT_DIR/progress"
    elif [ -f "$BOOT_DIR/progress" ]; then
        # A phase that carries no count clears the previous phase's count
        # rather than letting a stale "4210/8630" describe the wrong step.
        rm -f "$BOOT_DIR/progress"
    fi
    # World-readable: the probe runs as root today, but a healthcheck or the
    # agent's own SDK may read it tomorrow.
    chmod 0755 "$BOOT_DIR" 2>/dev/null
    chmod 0644 "$BOOT_DIR"/* 2>/dev/null
    printf '[%s] phase=%s %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$phase" \
        "${done_count:+${done_count}/${total_count:-?}}" >> "$BOOT_DIR/log"
} 2>/dev/null

exit 0
