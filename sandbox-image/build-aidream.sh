#!/usr/bin/env bash
# Build matrx-sandbox:aidream from a local aidream-current checkout.
#
# Why this script exists:
#   docker COPY only sees the build context, so we have to stage aidream's
#   source into the sandbox-image/ directory under aidream-src/. This script
#   does that with rsync + the targeted .dockerignore.aidream, captures the
#   git SHA, runs the build, and cleans up.
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
# 100+ commits stale (2026-07-09). Fetch is best-effort: offline builds bake
# the last-fetched origin/main rather than failing. Release callers pass an
# already-resolved full SHA so a moving main branch cannot change the staged
# source during a long build.
if [ -n "$SOURCE_SHA" ]; then
    [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || {
        echo "[build-aidream] invalid --source-sha: $SOURCE_SHA" >&2
        exit 1
    }
    git -C "$AIDREAM_SRC" cat-file -e "$SOURCE_SHA^{commit}" 2>/dev/null \
        || git -C "$AIDREAM_SRC" fetch origin "$SOURCE_SHA" --quiet \
        || {
            echo "[build-aidream] immutable source SHA is unavailable: $SOURCE_SHA" >&2
            exit 1
        }
    SOURCE_REF="$SOURCE_SHA"
else
    git -C "$AIDREAM_SRC" fetch origin main --quiet \
        || echo "[build-aidream] WARN: git fetch failed — baking last-known origin/main"
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
# Tracked-but-heavy dirs the sandbox variant doesn't need:
rm -rf "$STAGE_DIR/dashboard" "$STAGE_DIR/workflow-studio" "$STAGE_DIR/knowledgebase" \
       "$STAGE_DIR/.cursor" "$STAGE_DIR/.claude" "$STAGE_DIR/.agent" "$STAGE_DIR/.arman" "$STAGE_DIR/.treasure-maps"

# Keep the real immutable source commit as HEAD. A synthetic staging commit
# makes runtime/source certification impossible even when every file matches.
echo "[build-aidream] adding exact source commit for runtime certification"
(
    cd "$STAGE_DIR"
    if [ -d "$AIDREAM_SRC/.git" ]; then
        git init -q
        git remote add origin "$(git -C "$AIDREAM_SRC" remote get-url origin 2>/dev/null || echo '')"
        git fetch -q --depth=1 "$AIDREAM_SRC" "$AIDREAM_FULL_SHA"
        git reset -q --mixed FETCH_HEAD
        git branch -M main
        test "$(git rev-parse HEAD)" = "$AIDREAM_FULL_SHA"
        git diff --quiet
    fi
) || {
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

echo "[build-aidream] cleaning up staged source"
rm -rf "$STAGE_DIR" "$LOCAL_SCRIPTS_STAGE"

echo "[build-aidream] done — image: $TAG"
docker images "$TAG" --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}"
