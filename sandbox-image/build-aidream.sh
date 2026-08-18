#!/usr/bin/env bash
# Build matrx-sandbox:aidream from a local aidream-current checkout.
#
# Why this script exists:
#   docker COPY only sees the build context, so we have to stage aidream's
#   source into the sandbox-image/ directory under aidream-src/. This script
#   does that with `git archive` at the exact release commit (tracked files
#   only), seeds a .git whose HEAD IS that commit so the image can certify its
#   own runtime source, runs the build, and cleans up.
#
# Usage:
#   ./build-aidream.sh [/path/to/aidream]      # default: /srv/projects/aidream
#   ./build-aidream.sh --tag matrx-sandbox:aidream-edge /path/to/aidream
#   ./build-aidream.sh --source-sha <full-sha>  # immutable release build
#
# Prerequisites:
#   matrx-sandbox:core must already be built (`docker build -t matrx-sandbox:core .`).

set -euo pipefail

TAG="matrx-sandbox:aidream"
AIDREAM_SRC="/srv/projects/aidream"
SOURCE_SHA=""

while [ $# -gt 0 ]; do
    case "$1" in
        --tag) TAG="$2"; shift 2 ;;
        --source-sha) SOURCE_SHA="$2"; shift 2 ;;
        -h|--help) sed -n 's/^# \?//p' "$0" | head -20; exit 0 ;;
        *) AIDREAM_SRC="$1"; shift ;;
    esac
done

if [ ! -d "$AIDREAM_SRC" ]; then
    echo "[build-aidream] aidream source not found at $AIDREAM_SRC" >&2
    exit 1
fi
CORE_IMAGE="matrx-sandbox:${MATRX_CORE_VERSION:-core}"
if ! docker image inspect "$CORE_IMAGE" >/dev/null 2>&1; then
    echo "[build-aidream] $CORE_IMAGE not built" >&2
    exit 1
fi

cd "$(dirname "$0")"  # cd into sandbox-image/

# Bake origin/main, NOT the local working tree. /srv/projects/aidream is a
# reference clone with no auto-pull — building from its HEAD shipped images
# 100+ commits stale (2026-07-09). The Manager carries GITHUB_PAT but its
# non-interactive Git checkout has no credential helper, so retry through a
# short-lived askpass helper before refusing to build stale source. Release
# callers pass an already-resolved full SHA so a moving main branch cannot
# change the staged source during a long build.
fetch_with_manager_identity() {
    local -a fetch_args=("$@")
    if git -C "$AIDREAM_SRC" fetch "${fetch_args[@]}" --quiet; then
        return 0
    fi
    [[ -n "${GITHUB_PAT:-}" ]] || return 1

    local askpass_dir askpass
    askpass_dir="$(mktemp -d)"
    askpass="$askpass_dir/askpass.sh"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        'case "$1" in' \
        '  *Username*) printf "%s\\n" "x-access-token" ;;' \
        '  *) printf "%s\\n" "$GITHUB_PAT" ;;' \
        'esac' > "$askpass"
    chmod 700 "$askpass"
    local status=0
    GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0 \
        git -C "$AIDREAM_SRC" fetch "${fetch_args[@]}" --quiet || status=$?
    rm -rf "$askpass_dir"
    return "$status"
}

if [ -n "$SOURCE_SHA" ]; then
    [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || {
        echo "[build-aidream] invalid --source-sha: $SOURCE_SHA" >&2
        exit 1
    }
    git -C "$AIDREAM_SRC" cat-file -e "$SOURCE_SHA^{commit}" 2>/dev/null \
        || fetch_with_manager_identity origin "$SOURCE_SHA" \
        || {
            echo "[build-aidream] immutable source SHA is unavailable: $SOURCE_SHA" >&2
            exit 1
        }
    SOURCE_REF="$SOURCE_SHA"
else
    fetch_with_manager_identity origin main \
        || {
            echo "[build-aidream] current origin/main is unavailable; refusing to bake stale source" >&2
            exit 1
        }
    SOURCE_REF="origin/main"
fi
AIDREAM_FULL_SHA=$(git -C "$AIDREAM_SRC" rev-parse "$SOURCE_REF" 2>/dev/null || echo "unknown")
GIT_SHA=${AIDREAM_FULL_SHA:0:9}
STAGE_DIR="./aidream-src"
LOCAL_SCRIPTS_STAGE="./scripts-local"

# Stage entrypoint-local.sh + shutdown-local.sh from sandbox-local/ into the
# build context so the Dockerfile can COPY them. Docker COPY can't reach
# outside the build context (this dir).
echo "[build-aidream] staging hosted-tier entrypoint scripts into $LOCAL_SCRIPTS_STAGE"
rm -rf "$LOCAL_SCRIPTS_STAGE"
mkdir -p "$LOCAL_SCRIPTS_STAGE"
# -f: force overwrite if a stale/concurrent file is somehow present. Plain `cp`
# fails with "File exists" if two builds race against this staging dir; the
# Manager's rebuild-missing endpoint now also serializes builds via a lock,
# but this is the belt to the lock's suspenders.
cp -f ../sandbox-local/scripts/entrypoint-local.sh "$LOCAL_SCRIPTS_STAGE/"
cp -f ../sandbox-local/scripts/shutdown-local.sh "$LOCAL_SCRIPTS_STAGE/"

echo "[build-aidream] staging aidream source ($GIT_SHA) into $STAGE_DIR"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

# Stage tracked files from origin/main via `git archive` — reads the object
# store only, so the reference clone's working tree (and any uncommitted agent
# work in it) is never touched, and the image content matches GitHub exactly.
# Untracked junk (__pycache__, node_modules, .venv, .env) never ships because
# archive only emits tracked files.
git -C "$AIDREAM_SRC" archive --format=tar "$SOURCE_REF" | tar -x -C "$STAGE_DIR"

# DO NOT delete tracked paths from the staged tree (this used to strip
# knowledgebase/, .claude/, .arman/, tmp/, … to save ~6 MB of a multi-GB
# image). Since 76ea81c the image certifies its runtime source: the staged
# tree must be a byte-exact checkout of $AIDREAM_FULL_SHA, so any deleted
# tracked file makes `git status` dirty forever — which is exactly what
# wedged the hosted deploy poller for 20 h on 2026-08-11 (every run failed at
# "exact source commit staging failed"). Exactness beats 6 MB. `git archive`
# emits tracked files only, so there is no untracked junk to prune either.

# Keep the real immutable source commit as HEAD. A synthetic staging commit
# makes runtime/source certification impossible even when every file matches.
echo "[build-aidream] adding exact source commit for runtime certification"
stage_exact_commit() {
    cd "$STAGE_DIR" || return 1
    [ -d "$AIDREAM_SRC/.git" ] || return 0
    step() { echo "[build-aidream] staging step failed: $*" >&2; return 1; }
    git init -q || step git init
    git remote add origin \
        "$(git -C "$AIDREAM_SRC" remote get-url origin 2>/dev/null || echo '')" \
        || step git remote add origin
    git fetch -q --depth=1 "$AIDREAM_SRC" "$AIDREAM_FULL_SHA" \
        || step git fetch "$AIDREAM_FULL_SHA"
    git reset -q --mixed FETCH_HEAD || step git reset FETCH_HEAD
    git branch -M main || step git branch -M main
    test "$(git rev-parse HEAD)" = "$AIDREAM_FULL_SHA" \
        || step "HEAD $(git rev-parse HEAD) != source $AIDREAM_FULL_SHA"
    # Loud, not silent: name the files that broke exactness. A `git diff
    # --quiet` that only reported "failed" cost a full day of blind retries.
    if git diff --quiet; then return 0; fi
    echo "[build-aidream] staged tree differs from $AIDREAM_FULL_SHA — first 20 paths:" >&2
    git diff --name-status | head -20 >&2
    echo "[build-aidream] (nothing may delete or modify tracked files in $STAGE_DIR)" >&2
    return 1
}
( set -e; stage_exact_commit ) || {
    echo "[build-aidream] exact source commit staging failed" >&2
    exit 1
}

STAGED_SIZE=$(du -sh "$STAGE_DIR" | cut -f1)
echo "[build-aidream] staged $STAGED_SIZE"

echo "[build-aidream] docker build → $TAG (this is the slow step — uv sync takes 5–10 min first time)"
# Forward MATRX_IMAGE_VERSION (the orchestrator/sandbox-monorepo SHA) when the
# caller (deploy-hosted.sh / CI) sets it, so /etc/sandbox-image-version on the
# aidream variant matches core/slim and drift detection works. Defaults to "dev".
docker build \
    --label "com.aimatrx.aidream.sha=$AIDREAM_FULL_SHA" \
    -f Dockerfile.aidream \
    --build-arg AIDREAM_GIT_SHA="$AIDREAM_FULL_SHA" \
    --build-arg MATRX_IMAGE_VERSION="${MATRX_IMAGE_VERSION:-dev}" \
    --build-arg CORE_VERSION="${MATRX_CORE_VERSION:-core}" \
    -t "$TAG" \
    .

IMAGE_AIDREAM_SHA=$(docker image inspect "$TAG" --format '{{ index .Config.Labels "com.aimatrx.aidream.sha" }}')
[ "$IMAGE_AIDREAM_SHA" = "$AIDREAM_FULL_SHA" ] || {
    echo "[build-aidream] image aidream SHA label mismatch: $IMAGE_AIDREAM_SHA != $AIDREAM_FULL_SHA" >&2
    exit 1
}

echo "[build-aidream] verifying Claude Linux sandbox prerequisites in $TAG"
docker run --rm --entrypoint /bin/sh "$TAG" -c \
    'test "$(cat /etc/aidream-image-sha)" = "$(git -C /opt/aidream-template rev-parse HEAD)" \
    && command -v bwrap >/dev/null && command -v socat >/dev/null \
    && bwrap --version >/dev/null && socat -V >/dev/null 2>&1 \
    && runtime_dir=$(mktemp -d) \
    && cp -a /opt/aidream-template/. "$runtime_dir/" \
    && AIDREAM_WORK_DIR="$runtime_dir" /opt/sandbox/scripts/aidream-helpers.sh verify-release'

echo "[build-aidream] verifying immutable managed source as the runtime user"
docker run --rm --read-only \
    --tmpfs /home/agent:rw,nosuid,nodev,mode=0700,uid=1000,gid=1000 \
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
    --tmpfs /run:rw,nosuid,nodev,mode=1777 \
    --tmpfs /var/log/aidream:rw,nosuid,nodev,mode=0775,uid=1000,gid=1000 \
    --entrypoint /bin/sh "$TAG" -c \
    'set -eu \
    && mkdir -p /home/agent/.local/lib/python3.13/site-packages /run/aidream-managed-home \
    && printf "%s\n" "open(\"/tmp/sitecustomize-ran\", \"w\").write(\"bad\")" > /home/agent/.local/lib/python3.13/site-packages/sitecustomize.py \
    && printf "%s\n" "[core]" "  fsmonitor = !touch /tmp/gitconfig-ran #" > /home/agent/.gitconfig \
    && mkdir -p /home/agent/aidream/.venv/bin \
    && for shim in findmnt sudo env sleep bash; do printf "%s\n" "#!/bin/sh" "touch /tmp/shim-$shim-ran" "exit 99" > "/home/agent/aidream/.venv/bin/$shim"; chmod +x "/home/agent/aidream/.venv/bin/$shim"; done \
    && chown -R agent:agent /home/agent \
    && chmod 0555 /run/aidream-managed-home \
    && findmnt -n -o OPTIONS -T /opt/aidream-template | grep -Eq "(^|,)ro(,|$)" \
    && test ! -w /opt/aidream-template \
    && test ! -w /opt/aidream-template/pyproject.toml \
    && test ! -w /opt/aidream-template/.venv \
    && ! sudo -u agent sudo touch /opt/aidream-template/.sudo-write-probe 2>/dev/null \
    && ! sudo -u agent sudo touch /opt/aidream-template/.venv/.sudo-write-probe 2>/dev/null \
    && ! sudo -u agent sudo /bin/mount -o remount,rw / 2>/dev/null \
    && ! sudo -u agent sudo /bin/mount --bind /home/agent/aidream /opt/aidream-template 2>/dev/null \
    && sudo -u agent env -i \
        PATH=/opt/aidream-template/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        HOME=/run/aidream-managed-home GIT_CONFIG_GLOBAL=/dev/null \
        AIDREAM_WORK_DIR=/opt/aidream-template AIDREAM_IMAGE_SHA_FILE=/etc/aidream-image-sha \
        PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
        /bin/bash --noprofile --norc -p /opt/sandbox/scripts/aidream-helpers.sh verify-release \
    && sudo -u agent env -i HOME=/run/aidream-managed-home PYTHONNOUSERSITE=1 \
        /opt/aidream-template/.venv/bin/python -I -c "import sys; assert sys.flags.isolated and sys.flags.no_user_site" \
    && test ! -e /tmp/sitecustomize-ran \
    && test ! -e /tmp/gitconfig-ran \
    && ! compgen -G "/tmp/shim-*-ran" >/dev/null \
    && sudo -u agent touch /var/log/aidream/.agent-log-probe \
    && rm /var/log/aidream/.agent-log-probe'

echo "[build-aidream] cleaning up staged source"
rm -rf "$STAGE_DIR" "$LOCAL_SCRIPTS_STAGE"

echo "[build-aidream] done — image: $TAG"
docker images "$TAG" --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}"
