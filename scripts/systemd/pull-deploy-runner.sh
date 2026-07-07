#!/usr/bin/env bash
# Poller entrypoint for the hosted-tier pull deploy. Installed OUTSIDE the
# repo (install.sh copies it to /usr/local/bin) on purpose: it git-resets the
# checkout below, and a script must never rewrite itself mid-execution.
#
# Under the main deploy lock: sync the checkout to origin/main FIRST, then run
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
  OLD=$(git rev-parse HEAD 2>/dev/null || echo none)
  git fetch origin main --quiet
  git reset --hard origin/main --quiet
  chmod +x scripts/deploy-hosted.sh
  DEPLOY_LOCK_FILE="'"$LOCK_DIR"'/.deploy-hosted.inner.lock" \
    PRE_SYNCED_OLD_SHA="$OLD" bash scripts/deploy-hosted.sh
'
