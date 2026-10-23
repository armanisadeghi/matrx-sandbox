#!/usr/bin/env bash
# Poller entrypoint for the hosted-tier pull deploy. Installed OUTSIDE the
# repo (install.sh copies it to /usr/local/bin) on purpose: it git-resets the
# checkout below, and a script must never rewrite itself mid-execution.
#
# Under the main deploy lock: sync to the CI-controlled deploy ref, then run
# the freshly-landed deploy-hosted.sh. The GitHub fast path runs THIS runner
# too (with MATRX_SANDBOX_EXPECT_SHA), so there is one sync path, not two.
# deploy-hosted.sh's own state-file diff decides whether anything rebuilds, so
# an idle tick costs one git fetch and exits.
#
# Where the source lives is host config, not code: /etc/matrx/sandbox-release.env
# (optional; absent = today's standalone repository). The checkout's own git
# config carries its credential (repo-local core.sshCommand deploy key, the
# same pattern /srv/projects/aidream uses), so this script never handles one.
#   MATRX_SANDBOX_CHECKOUT    git checkout root (default /srv/projects/matrx-sandbox)
#   MATRX_SANDBOX_SUBDIR      sandbox root inside it ("" today; infra/matrx-sandbox in aidream)
#   MATRX_SANDBOX_DEPLOY_REF  approved ref; default follows the layout exactly as
#                             scripts/lib/release-guard.sh release_source_init does
set -euo pipefail

ENV_FILE="${MATRX_SANDBOX_RELEASE_ENV:-/etc/matrx/sandbox-release.env}"
if [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

CHECKOUT="${MATRX_SANDBOX_CHECKOUT:-${MATRX_SANDBOX_DIR:-/srv/projects/matrx-sandbox}}"
SUBDIR="${MATRX_SANDBOX_SUBDIR:-}"
SUBDIR="${SUBDIR%/}"
if [ -z "$SUBDIR" ]; then
  DEPLOY_REF="${MATRX_SANDBOX_DEPLOY_REF:-refs/heads/deploy/hosted}"
else
  DEPLOY_REF="${MATRX_SANDBOX_DEPLOY_REF:-refs/heads/deploy/sandbox-hosted}"
fi
SANDBOX_ROOT="$CHECKOUT${SUBDIR:+/$SUBDIR}"
LOCK_DIR="${MATRX_SANDBOX_LOCK_DIR:-/srv/apps/deploy-state}"

export RUNNER_CHECKOUT="$CHECKOUT" RUNNER_SANDBOX_ROOT="$SANDBOX_ROOT" \
  RUNNER_DEPLOY_REF="$DEPLOY_REF" RUNNER_LOCK_DIR="$LOCK_DIR" \
  RUNNER_EXPECT_SHA="${MATRX_SANDBOX_EXPECT_SHA:-}"

mkdir -p "$LOCK_DIR"
exec flock "$LOCK_DIR/.deploy-hosted.lock" bash -c '
  set -euo pipefail
  cd "$RUNNER_CHECKOUT"
  if ! git fetch origin "$RUNNER_DEPLOY_REF" --quiet 2>/dev/null; then
    # No approved release exists yet (the promotion gate has not passed since
    # the ref was created, or it was removed). HOLD politely — fataling here
    # spammed the journal every 2 minutes and looked like a poller crash when
    # it was actually the gate upstream doing its job. The line names the
    # remote and ref so a wrong source (an archived repo, a ref typo) shows.
    echo "[pull-deploy-runner] $RUNNER_DEPLOY_REF not found on $(git remote get-url origin 2>/dev/null | sed -E "s#//[^@/]*@#//#") — no approved release yet; holding."
    [ -z "$RUNNER_EXPECT_SHA" ] || exit 1
    exit 0
  fi
  TARGET=$(git rev-parse FETCH_HEAD)
  [[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid approved SHA: $TARGET" >&2; exit 1; }
  if [ -n "$RUNNER_EXPECT_SHA" ] && [ "$TARGET" != "$RUNNER_EXPECT_SHA" ]; then
    echo "[pull-deploy-runner] $RUNNER_DEPLOY_REF is $TARGET, caller expected $RUNNER_EXPECT_SHA" >&2
    exit 1
  fi
  git checkout --detach "$TARGET" --quiet
  chmod +x "$RUNNER_SANDBOX_ROOT/scripts/deploy-hosted.sh"
  MATRX_SANDBOX_DIR="$RUNNER_SANDBOX_ROOT" \
    MATRX_SANDBOX_DEPLOY_REF="$RUNNER_DEPLOY_REF" \
    DEPLOY_LOCK_FILE="$RUNNER_LOCK_DIR/.deploy-hosted.inner.lock" \
    DEPLOY_TARGET_SHA="$TARGET" bash "$RUNNER_SANDBOX_ROOT/scripts/deploy-hosted.sh"
'
