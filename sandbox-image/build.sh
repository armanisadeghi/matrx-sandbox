#!/usr/bin/env bash
# Single source of truth for building matrx-sandbox images WITH a version stamp.
#
# The zero-drift system needs every image to carry MATRX_IMAGE_VERSION (baked
# as an ENV + LABEL + /etc/sandbox-image-version). A bare `docker build` leaves
# it "dev", which the orchestrator's drift report flags as unversioned — so
# ALWAYS build through this script.
#
# Usage:
#   ./build.sh [core|slim|aidream|all]      # default: core
#   MATRX_IMAGE_VERSION=v1.2.3 ./build.sh   # pin an explicit version
#   AIDREAM_SRC=/path ./build.sh aidream    # aidream source override
#
# Version format (auto): <short-sha>[-dirty]-<utcYYYYmmddHHMMSS>
#   - short-sha: traceable back to the exact matrx-sandbox commit
#   - -dirty:    set when the working tree has uncommitted changes (a built-from-
#                dirty-tree image is, by definition, drift-prone — surface it)
#   - timestamp: makes every rebuild a distinct version even at the same commit
set -euo pipefail
cd "$(dirname "$0")"   # sandbox-image/

VARIANT="${1:-core}"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo /srv/projects/matrx-sandbox)"
SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
DIRTY=""
git -C "$REPO_ROOT" diff --quiet 2>/dev/null || DIRTY="-dirty"
TS="$(date -u +%Y%m%d%H%M%S)"
VERSION="${MATRX_IMAGE_VERSION:-${SHA}${DIRTY}-${TS}}"
export MATRX_IMAGE_VERSION="$VERSION"
echo "[build] MATRX_IMAGE_VERSION=$VERSION  variant=$VARIANT"

build_core()    { docker build    --build-arg MATRX_IMAGE_VERSION="$VERSION" -t matrx-sandbox:core . ; }
build_slim()    { docker build -f Dockerfile.slim --build-arg MATRX_IMAGE_VERSION="$VERSION" -t matrx-sandbox:slim . ; }
# :aidream is FROM :core, so it inherits core's ENV/LABEL/-version file; rebuild
# core first so the inherited stamp is current.
build_aidream() { build_core; ./build-aidream.sh "${AIDREAM_SRC:-/srv/projects/aidream}" ; }

case "$VARIANT" in
  core)    build_core ;;
  slim)    build_slim ;;
  aidream) build_aidream ;;
  all)     build_core; build_slim; build_aidream ;;
  *) echo "usage: build.sh [core|slim|aidream|all]" >&2; exit 1 ;;
esac
echo "[build] done: $VARIANT @ $VERSION"
