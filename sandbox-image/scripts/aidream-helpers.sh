#!/usr/bin/env bash
# `mtx aidream <subcommand>` — manage the aidream working copy inside a
# sandbox spawned from matrx-sandbox:aidream.
#
# Subcommands:
#   update    git pull origin main + uv sync --frozen against /home/agent/aidream
#   reset     wipe /home/agent/aidream and re-copy from /opt/aidream-template
#             (DESTRUCTIVE — surfaces a confirmation prompt unless --force)
#   serve     start aidream's FastAPI on port 8001 in the background; logs
#             to /var/log/sandbox/aidream-server.log
#   stop      stop the running aidream FastAPI (if any)
#   status    show whether the FastAPI is up + last update time
#   verify-release  require a clean working copy at the image's exact full SHA
#   version   print git sha of the working copy + the image's bake-time sha

set -uo pipefail

WORK_DIR="${AIDREAM_WORK_DIR:-/home/agent/aidream}"
TEMPLATE_DIR="${AIDREAM_TEMPLATE_DIR:-/opt/aidream-template}"
LOG_DIR="/var/log/sandbox"
PID_FILE="$LOG_DIR/aidream-server.pid"
LOG_FILE="$LOG_DIR/aidream-server.log"
SERVE_PORT="${AIDREAM_SERVE_PORT:-8001}"
IMAGE_SHA_FILE="${AIDREAM_IMAGE_SHA_FILE:-/etc/aidream-image-sha}"

cmd="${1:-}"
shift || true

usage() {
    cat <<EOF
mtx aidream — manage the aidream working copy in this sandbox.

Subcommands:
  update                Pull latest aidream main + uv sync the venv.
  reset [--force]       Wipe ~/aidream and re-copy from the image template.
                        Asks for confirmation unless --force is passed.
  serve [--port N] [--require-image-source]
                        Start aidream FastAPI in background (default port 8001).
                        The image entrypoint requires exact baked source.
  stop                  Stop the running aidream FastAPI.
  status                Show server status + last update.
  verify-release        Require clean working copy at the image's exact full SHA.
  version               Show working-copy git sha + image bake-time sha.
EOF
}

require_workdir() {
    if [ ! -d "$WORK_DIR" ]; then
        echo "[mtx aidream] $WORK_DIR not found — image may not be matrx-sandbox:aidream" >&2
        exit 1
    fi
}

verify_release_source() {
    require_workdir
    local expected actual dirty
    expected=$(cat "$IMAGE_SHA_FILE" 2>/dev/null || true)
    actual=$(git -C "$WORK_DIR" rev-parse HEAD 2>/dev/null || true)
    dirty=$(git -C "$WORK_DIR" status --porcelain --untracked-files=all 2>/dev/null || true)
    if ! [[ "$expected" =~ ^[0-9a-f]{40}$ ]]; then
        echo "source_state=invalid-image-sha expected=$expected" >&2
        return 1
    fi
    if [ "$actual" != "$expected" ]; then
        echo "source_state=sha-mismatch expected=$expected actual=${actual:-missing}" >&2
        return 1
    fi
    if [ -n "$dirty" ]; then
        # Name the offending paths. A bare "modified" sent a full day of
        # hosted deploys into blind retries on 2026-08-11 — the image build was
        # deleting tracked dirs from the very tree it then certified.
        echo "source_state=modified expected=$expected actual=$actual" >&2
        echo "$dirty" | head -20 >&2
        return 1
    fi
    echo "source_state=exact expected=$expected actual=$actual"
}

cmd_update() {
    require_workdir
    cd "$WORK_DIR" || exit 1
    if [ -d .git ]; then
        echo "[mtx aidream] git pull..."
        git pull --ff-only || { echo "[mtx aidream] git pull failed"; exit 1; }
    else
        echo "[mtx aidream] no .git in working copy — image was baked without git history."
        echo "                Use 'mtx aidream reset' to refresh from the (possibly newer) image template."
        exit 1
    fi
    echo "[mtx aidream] uv sync..."
    uv sync --frozen || { echo "[mtx aidream] uv sync failed"; exit 1; }
    echo "[mtx aidream] up to date: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
}

cmd_reset() {
    local force=0
    [ "${1:-}" = "--force" ] && force=1
    if [ "$force" -ne 1 ]; then
        read -r -p "This will DELETE $WORK_DIR and re-copy from $TEMPLATE_DIR. Continue? [y/N] " ans
        case "$ans" in [yY]|[yY][eE][sS]) ;; *) echo "aborted"; exit 0 ;; esac
    fi
    cmd_stop >/dev/null 2>&1 || true
    sudo rm -rf "$WORK_DIR"
    sudo cp -a "$TEMPLATE_DIR" "$WORK_DIR"
    sudo chown -R agent:agent "$WORK_DIR"
    echo "[mtx aidream] reset complete: $(du -sh "$WORK_DIR" | cut -f1)"
}

cmd_serve() {
    require_workdir
    local port="$SERVE_PORT"
    local require_image_source=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --port) port="$2"; shift 2;;
            --require-image-source) require_image_source=1; shift;;
            *) shift;;
        esac
    done
    if [ "$require_image_source" -eq 1 ]; then
        verify_release_source || {
            echo "[mtx aidream] refusing managed autostart from stale or modified source" >&2
            exit 1
        }
    fi
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[mtx aidream] already running (pid=$(cat "$PID_FILE"), port=$port)"
        return 0
    fi
    sudo mkdir -p "$LOG_DIR" && sudo chown agent:agent "$LOG_DIR"
    cd "$WORK_DIR" || exit 1
    # MUST go through run.py — it calls aidream.package_integration which
    # invokes matrx_ai.configure(...) to register the host's DB models with
    # matrx-ai. Bypassing it (e.g. direct uvicorn aidream.api.app:fastapi_app)
    # crashes at first DB-touching import with DBNotConfiguredError because
    # ContentBlocks etc. were never registered.
    #
    # run.py reads PORT env (defaults to 8000) and falls back to a random
    # free port when the preferred port is in use. We set PORT=$port AND
    # ensure $port is free (8001 is free by convention; matrx_agent has 8000)
    # so the bind is deterministic — required for the orchestrator's path
    # routing in /proxy/{path:path} to know where to send /ai/* and /api/*.
    if [ "$require_image_source" -eq 1 ]; then
        # The managed image already contains a fully resolved venv. Execute
        # its fixed interpreter directly so startup cannot mutate the
        # root-owned template or redirect uv through user-controlled config.
        PORT="$port" PYTHONDONTWRITEBYTECODE=1 \
            nohup "$WORK_DIR/.venv/bin/python" -I run.py >"$LOG_FILE" 2>&1 &
    else
        PORT="$port" nohup uv run python run.py >"$LOG_FILE" 2>&1 &
    fi
    echo $! > "$PID_FILE"
    sleep 2
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[mtx aidream] started (pid=$(cat "$PID_FILE"), port=$port)"
        echo "                logs: $LOG_FILE"
    else
        echo "[mtx aidream] failed to start — see $LOG_FILE" >&2
        tail -20 "$LOG_FILE" >&2
        exit 1
    fi
}

cmd_stop() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid"
            for _ in $(seq 1 20); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
            echo "[mtx aidream] stopped pid=$pid"
        fi
        rm -f "$PID_FILE"
    else
        echo "[mtx aidream] not running"
    fi
}

cmd_status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "running pid=$(cat "$PID_FILE") log=$LOG_FILE"
    else
        echo "stopped"
    fi
    if [ -d "$WORK_DIR/.git" ]; then
        echo "working_copy_sha=$(cd "$WORK_DIR" && git rev-parse HEAD 2>/dev/null)"
    fi
    echo "image_bake_sha=$(cat "$IMAGE_SHA_FILE" 2>/dev/null || echo unknown)"
    verify_release_source 2>&1 || true
}

cmd_version() { cmd_status; }

case "$cmd" in
    update)  cmd_update "$@" ;;
    reset)   cmd_reset "$@" ;;
    serve)   cmd_serve "$@" ;;
    stop)    cmd_stop "$@" ;;
    status)  cmd_status "$@" ;;
    verify-release) verify_release_source ;;
    version) cmd_version "$@" ;;
    -h|--help|"") usage ;;
    *) echo "unknown subcommand: $cmd" >&2; usage; exit 2 ;;
esac
