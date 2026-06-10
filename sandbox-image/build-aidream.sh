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
#
# Prerequisites:
#   matrx-sandbox:core must already be built (`docker build -t matrx-sandbox:core .`).

set -euo pipefail

TAG="matrx-sandbox:aidream"
AIDREAM_SRC="/srv/projects/aidream"

while [ $# -gt 0 ]; do
    case "$1" in
        --tag) TAG="$2"; shift 2 ;;
        -h|--help) sed -n 's/^# \?//p' "$0" | head -20; exit 0 ;;
        *) AIDREAM_SRC="$1"; shift ;;
    esac
done

if [ ! -d "$AIDREAM_SRC" ]; then
    echo "[build-aidream] aidream source not found at $AIDREAM_SRC" >&2
    exit 1
fi
if ! docker image inspect matrx-sandbox:core >/dev/null 2>&1; then
    echo "[build-aidream] matrx-sandbox:core not built — run 'docker build -t matrx-sandbox:core sandbox-image/' first" >&2
    exit 1
fi

cd "$(dirname "$0")"  # cd into sandbox-image/

GIT_SHA=$(git -C "$AIDREAM_SRC" rev-parse --short HEAD 2>/dev/null || echo "unknown")
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

# Mirror only what we need; .dockerignore.aidream documents intent but
# rsync's --exclude is what actually filters on copy.
rsync -a \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='.cursor' \
    --exclude='.claude' \
    --exclude='.agent' \
    --exclude='.arman' \
    --exclude='.treasure-maps' \
    --exclude='node_modules' \
    --exclude='.venv' \
    --exclude='.next' \
    --exclude='dist/' \
    --exclude='build/' \
    --exclude='tmp/' \
    --exclude='dashboard/' \
    --exclude='workflow-studio/' \
    --exclude='knowledgebase/' \
    "$AIDREAM_SRC/" "$STAGE_DIR/"

# We DO want a .git dir for `mtx aidream update` to work — but we want
# the small one (refs + remotes only), not the full history. Use a
# git clone --depth=1 trick: re-init in the staged dir as a shallow
# clone of the source.
echo "[build-aidream] adding shallow .git so 'mtx aidream update' works"
(
    cd "$STAGE_DIR"
    if [ -d "$AIDREAM_SRC/.git" ]; then
        # Shallow-init from the source repo's remotes.
        git init -q
        git remote add origin "$(git -C "$AIDREAM_SRC" remote get-url origin 2>/dev/null || echo '')"
        # Pretend the current state is HEAD so `git pull` later does the right thing.
        git add -A 2>/dev/null
        git -c user.email=build@matrx -c user.name=build commit -q -m "bake: aidream@$GIT_SHA" 2>/dev/null || true
    fi
) || echo "[build-aidream] WARN: .git seeding failed; mtx aidream update may not work until first reset"

STAGED_SIZE=$(du -sh "$STAGE_DIR" | cut -f1)
echo "[build-aidream] staged $STAGED_SIZE"

echo "[build-aidream] docker build → $TAG (this is the slow step — uv sync takes 5–10 min first time)"
# Forward MATRX_IMAGE_VERSION (the orchestrator/sandbox-monorepo SHA) when the
# caller (deploy-hosted.sh / CI) sets it, so /etc/sandbox-image-version on the
# aidream variant matches core/slim and drift detection works. Defaults to "dev".
docker build \
    -f Dockerfile.aidream \
    --build-arg AIDREAM_GIT_SHA="$GIT_SHA" \
    --build-arg MATRX_IMAGE_VERSION="${MATRX_IMAGE_VERSION:-dev}" \
    -t "$TAG" \
    .

echo "[build-aidream] cleaning up staged source"
rm -rf "$STAGE_DIR" "$LOCAL_SCRIPTS_STAGE"

echo "[build-aidream] done — image: $TAG"
docker images "$TAG" --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}"
