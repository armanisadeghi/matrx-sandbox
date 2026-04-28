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
#   version   print git sha of the working copy + the image's bake-time sha

set -uo pipefail

WORK_DIR="${AIDREAM_WORK_DIR:-/home/agent/aidream}"
TEMPLATE_DIR="${AIDREAM_TEMPLATE_DIR:-/opt/aidream-template}"
LOG_DIR="/var/log/sandbox"
PID_FILE="$LOG_DIR/aidream-server.pid"
LOG_FILE="$LOG_DIR/aidream-server.log"
SERVE_PORT="${AIDREAM_SERVE_PORT:-8001}"

cmd="${1:-}"
shift || true

usage() {
    cat <<EOF
mtx aidream — manage the aidream working copy in this sandbox.

Subcommands:
  update                Pull latest aidream main + uv sync the venv.
  reset [--force]       Wipe ~/aidream and re-copy from the image template.
                        Asks for confirmation unless --force is passed.
  serve [--port N]      Start aidream FastAPI in background (default port 8001).
  stop                  Stop the running aidream FastAPI.
  status                Show server status + last update.
  version               Show working-copy git sha + image bake-time sha.
EOF
}

require_workdir() {
    if [ ! -d "$WORK_DIR" ]; then
        echo "[mtx aidream] $WORK_DIR not found — image may not be matrx-sandbox:aidream" >&2
        exit 1
    fi
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
    while [ $# -gt 0 ]; do
        case "$1" in
            --port) port="$2"; shift 2;;
            *) shift;;
        esac
    done
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[mtx aidream] already running (pid=$(cat "$PID_FILE"), port=$port)"
        return 0
    fi
    sudo mkdir -p "$LOG_DIR" && sudo chown agent:agent "$LOG_DIR"
    cd "$WORK_DIR" || exit 1
    # ALWAYS go through uvicorn directly so we pin the port. aidream's run.py
    # has its own "find a free port" logic that picks something random when
    # 8000 is busy (matrx_agent has 8000); we need 8001 to be stable so the
    # orchestrator's /proxy/{path:path} can route /ai/* to it deterministically.
    nohup uv run uvicorn aidream.api.app:fastapi_app --host 0.0.0.0 --port "$port" >"$LOG_FILE" 2>&1 &
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
        echo "working_copy_sha=$(cd "$WORK_DIR" && git rev-parse --short HEAD 2>/dev/null)"
    fi
    echo "image_bake_sha=${AIDREAM_GIT_SHA:-unknown}"
}

cmd_version() { cmd_status; }

case "$cmd" in
    update)  cmd_update "$@" ;;
    reset)   cmd_reset "$@" ;;
    serve)   cmd_serve "$@" ;;
    stop)    cmd_stop "$@" ;;
    status)  cmd_status "$@" ;;
    version) cmd_version "$@" ;;
    -h|--help|"") usage ;;
    *) echo "unknown subcommand: $cmd" >&2; usage; exit 2 ;;
esac
