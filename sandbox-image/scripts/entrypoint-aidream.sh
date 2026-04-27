#!/usr/bin/env bash
# Entrypoint for matrx-sandbox:aidream.
#
# Wraps the standard sandbox entrypoint with one extra step: ensure the
# user's persistent volume has an /home/agent/aidream working copy, seeded
# from /opt/aidream-template if absent. Subsequent spawns reuse whatever
# the user has at /home/agent/aidream — including any local edits.

set -uo pipefail

TEMPLATE_DIR="${AIDREAM_TEMPLATE_DIR:-/opt/aidream-template}"
WORK_DIR="/home/agent/aidream"

log() { echo "[entrypoint-aidream] $*"; }

if [ ! -d "$WORK_DIR" ] || [ -z "$(ls -A "$WORK_DIR" 2>/dev/null)" ]; then
    log "first spawn — seeding $WORK_DIR from $TEMPLATE_DIR"
    if [ ! -d "$TEMPLATE_DIR" ]; then
        log "WARNING: template dir $TEMPLATE_DIR missing; aidream will not be available"
    else
        # cp -a preserves permissions, hardlinks where possible, and the .venv.
        # The .venv inside the template uses absolute paths so it'll keep
        # working from $WORK_DIR/.venv.
        sudo -E cp -a "$TEMPLATE_DIR/." "$WORK_DIR/" || log "WARN: seed copy failed"
        sudo -E chown -R agent:agent "$WORK_DIR" || true
        log "seeded $(du -sh "$WORK_DIR" 2>/dev/null | cut -f1) into $WORK_DIR"
    fi
else
    log "found existing $WORK_DIR — preserving user state"
fi

# Hand off to the standard entrypoint (daemon, ttyd, cloud-files-sync,
# persistence module — everything :core does).
log "handing off to /opt/sandbox/scripts/entrypoint.sh"
exec /opt/sandbox/scripts/entrypoint.sh "$@"
