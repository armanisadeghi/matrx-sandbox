#!/usr/bin/env bash
# Entrypoint for matrx-sandbox:aidream.
#
# Wraps the standard sandbox entrypoint with one extra step: ensure the
# user's persistent volume has an /home/agent/aidream working copy, seeded
# from /opt/aidream-template if absent. Subsequent spawns reuse whatever
# the user has at /home/agent/aidream — including any local edits.

set -uo pipefail

# Never resolve an entrypoint primitive through the persistent user's venv or
# shell setup. Managed launch uses absolute binaries below; this PATH protects
# the seeding/downstream chain as well.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

TEMPLATE_DIR="/opt/aidream-template"
WORK_DIR="/home/agent/aidream"

log() { echo "[entrypoint-aidream] $*"; }

template_mount_is_read_only() {
    local options
    options=$(/usr/bin/findmnt -n -o OPTIONS -T "$TEMPLATE_DIR" 2>/dev/null || true)
    [[ ",$options," == *,ro,* ]]
}

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
        /bin/sed -i "s|$TEMPLATE_DIR|$WORK_DIR|g" "$pth"
        count=$((count + 1))
    done
    log "retargeted $count editable .pth file(s) to $WORK_DIR"
}

if [ ! -d "$WORK_DIR" ] || [ -z "$(/bin/ls -A "$WORK_DIR" 2>/dev/null)" ]; then
    log "first spawn — seeding $WORK_DIR from $TEMPLATE_DIR"
    if [ ! -d "$TEMPLATE_DIR" ]; then
        log "WARNING: template dir $TEMPLATE_DIR missing; aidream will not be available"
    else
        # cp -a preserves permissions and the .venv. uv editable .pth files
        # still need their absolute paths rewritten to point at $WORK_DIR
        # (handled below by retarget_editables).
        /usr/bin/sudo -E /bin/cp -a "$TEMPLATE_DIR/." "$WORK_DIR/" || log "WARN: seed copy failed"
        /usr/bin/sudo -E /usr/bin/chown -R agent:agent "$WORK_DIR" || true
        retarget_editables
        log "seeded $(/usr/bin/du -sh "$WORK_DIR" 2>/dev/null | /usr/bin/cut -f1) into $WORK_DIR"
    fi
else
    log "found existing $WORK_DIR — preserving user state"
    # Defensive: if the user's .pth files point at the template (because they
    # were created by an older entrypoint that didn't retarget), fix them now.
    if /usr/bin/grep -q "$TEMPLATE_DIR" "$WORK_DIR/.venv/lib/python3.13/site-packages"/_editable_impl_*.pth 2>/dev/null; then
        log "detected stale editable .pth files from older seed — retargeting"
        retarget_editables
    fi
fi

# Auto-start aidream's FastAPI on port 8001 after the standard daemon is up.
# Background it so we don't block; the orchestrator's /proxy/{path:path}
# routes /ai/* and /api/* paths here while leaving everything else (fs, git,
# exec) on matrx_agent at port 8000.
#
# The server always executes the immutable, image-certified template. The
# durable /home/agent/aidream checkout remains the user's editable worktree;
# it may intentionally be older, newer, or dirty and must never be reset just
# to boot the managed API. Run as the `agent` user (this entrypoint runs as
# root pre-exec). Wait 8s so matrx_agent has bound :8000 and the volume is
# fully seeded. The fixed, root-owned command is executed directly: no login
# or interactive shell may source user-controlled profile files. Shell hook
# variables and Python injection variables are removed, while the container's
# required platform/database/provider environment remains available. Failure
# here is non-fatal — the sandbox still works for fs/git/exec; only AI
# passthrough breaks.
if [ "${MATRX_TIER:-}" = "hosted" ]; then
  log "auto-starting aidream FastAPI on :8001 in the background"
  (
    /bin/sleep 8
    if ! template_mount_is_read_only; then
        log "ERROR: refusing managed aidream autostart: $TEMPLATE_DIR is not on a read-only mount"
        exit 1
    fi
    /usr/bin/mkdir -p /run/aidream-managed-home
    /usr/bin/chown root:root /run/aidream-managed-home
    /usr/bin/chmod 0555 /run/aidream-managed-home
    /usr/bin/sudo -u agent -E -H /usr/bin/env \
        -u BASH_ENV -u ENV -u PYTHONHOME -u PYTHONPATH -u PYTHONSTARTUP \
        -u LD_PRELOAD -u LD_LIBRARY_PATH \
        -u GIT_CONFIG_SYSTEM -u GIT_CONFIG_NOSYSTEM -u GIT_CONFIG_COUNT \
        -u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT -u UV_PYTHON \
        PATH="$TEMPLATE_DIR/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
        HOME=/run/aidream-managed-home \
        GIT_CONFIG_GLOBAL=/dev/null \
        AIDREAM_WORK_DIR="$TEMPLATE_DIR" \
        AIDREAM_TEMPLATE_DIR="$TEMPLATE_DIR" \
        AIDREAM_IMAGE_SHA_FILE=/etc/aidream-image-sha \
        PYTHONNOUSERSITE=1 \
        PYTHONDONTWRITEBYTECODE=1 \
        /bin/bash --noprofile --norc -p \
        /opt/sandbox/scripts/aidream-helpers.sh serve --port 8001 --require-image-source \
        > /var/log/sandbox/aidream-autostart.log 2>&1 || true
  ) &
  disown || true
else
  log "managed aidream API disabled: Claude managed runtime is hosted-only (tier=${MATRX_TIER:-unset})"
fi

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
