#!/usr/bin/env bash
# Poller entrypoint for the hosted-tier pull deploy. Installed OUTSIDE the
# repo (install.sh copies it to /usr/local/bin) on purpose: it git-resets the
# checkout below, and a script must never rewrite itself mid-execution.
#
# Under the main deploy lock: sync to the CI-controlled deploy/hosted ref, then run
# the freshly-landed deploy-hosted.sh (same pattern as the GHA SSH fast path).
# deploy-hosted.sh's own state-file diff decides whether anything rebuilds, so
# an idle tick costs one git fetch and exits.
set -euo pipefail

REPO_DIR="${MATRX_SANDBOX_DIR:-/srv/projects/matrx-sandbox}"
LOCK_DIR=/srv/apps/deploy-state

mkdir -p "$LOCK_DIR"
exec flock "$LOCK_DIR/.deploy-hosted.lock" bash -c '
  set -euo pipefail
  cd "'"$REPO_DIR"'"
  git fetch origin refs/heads/deploy/hosted --quiet
  TARGET=$(git rev-parse FETCH_HEAD)
  case "$TARGET" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
    *) echo "invalid approved SHA: $TARGET" >&2; exit 1 ;;
  esac
  git checkout --detach "$TARGET" --quiet
  chmod +x scripts/deploy-hosted.sh
  DEPLOY_LOCK_FILE="'"$LOCK_DIR"'/.deploy-hosted.inner.lock" \
    DEPLOY_TARGET_SHA="$TARGET" bash scripts/deploy-hosted.sh
'
