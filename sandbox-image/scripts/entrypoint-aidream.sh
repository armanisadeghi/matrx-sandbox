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

retarget_editables() {
    # uv editable installs use _editable_impl_<pkg>.pth files containing the
    # absolute path to the package source. After cp -a, those still point at
    # /opt/aidream-template — meaning `import matrx_ai` resolves to the
    # template, NOT the user's working copy, so edits don't take effect.
    # Rewrite each .pth file in place to point at $WORK_DIR.
    local site_pkgs="$WORK_DIR/.venv/lib/python3.13/site-packages"
    if [ ! -d "$site_pkgs" ]; then
        log "no .venv site-packages at $site_pkgs — skipping editable retarget"
        return
    fi
    local count=0
    for pth in "$site_pkgs"/_editable_impl_*.pth; do
        [ -f "$pth" ] || continue
        sed -i "s|$TEMPLATE_DIR|$WORK_DIR|g" "$pth"
        count=$((count + 1))
    done
    log "retargeted $count editable .pth file(s) to $WORK_DIR"
}

if [ ! -d "$WORK_DIR" ] || [ -z "$(ls -A "$WORK_DIR" 2>/dev/null)" ]; then
    log "first spawn — seeding $WORK_DIR from $TEMPLATE_DIR"
    if [ ! -d "$TEMPLATE_DIR" ]; then
        log "WARNING: template dir $TEMPLATE_DIR missing; aidream will not be available"
    else
        # cp -a preserves permissions and the .venv. uv editable .pth files
        # still need their absolute paths rewritten to point at $WORK_DIR
        # (handled below by retarget_editables).
        sudo -E cp -a "$TEMPLATE_DIR/." "$WORK_DIR/" || log "WARN: seed copy failed"
        sudo -E chown -R agent:agent "$WORK_DIR" || true
        retarget_editables
        log "seeded $(du -sh "$WORK_DIR" 2>/dev/null | cut -f1) into $WORK_DIR"
    fi
else
    log "found existing $WORK_DIR — preserving user state"
    # Defensive: if the user's .pth files point at the template (because they
    # were created by an older entrypoint that didn't retarget), fix them now.
    if grep -q "$TEMPLATE_DIR" "$WORK_DIR/.venv/lib/python3.13/site-packages"/_editable_impl_*.pth 2>/dev/null; then
        log "detected stale editable .pth files from older seed — retargeting"
        retarget_editables
    fi
fi

# Auto-start aidream's FastAPI on port 8001 after the standard daemon is up.
# Background it so we don't block; the orchestrator's /proxy/{path:path}
# routes /ai/* and /api/* paths here while leaving everything else (fs, git,
# exec) on matrx_agent at port 8000.
#
# Run as the `agent` user (this entrypoint runs as root pre-exec). Wait 8s
# so matrx_agent has bound :8000 and the volume is fully seeded before we
# start uvicorn against the venv. Failure here is non-fatal — the sandbox
# still works for fs/git/exec; only AI passthrough breaks.
log "auto-starting aidream FastAPI on :8001 in the background"
(
    sleep 8
    sudo -u agent -E -H bash -lc \
        '/usr/local/bin/mtx aidream serve --port 8001 --require-image-source' \
        > /var/log/sandbox/aidream-autostart.log 2>&1 || true
) &
disown || true

# Hand off to the right downstream entrypoint based on tier. Production
# entrypoint.sh requires S3_BUCKET (it runs hot-sync), which the hosted tier
# doesn't set; entrypoint-local.sh skips S3 and is what :local uses.
if [ -n "${S3_BUCKET:-}" ]; then
    log "handing off to /opt/sandbox/scripts/entrypoint.sh (production / S3 hot-sync)"
    exec /opt/sandbox/scripts/entrypoint.sh "$@"
elif [ -x /opt/sandbox/scripts/entrypoint-local.sh ]; then
    log "handing off to /opt/sandbox/scripts/entrypoint-local.sh (no S3 → hosted tier)"
    exec /opt/sandbox/scripts/entrypoint-local.sh "$@"
else
    log "ERROR: no S3_BUCKET set AND entrypoint-local.sh not present in image"
    exit 1
fi
