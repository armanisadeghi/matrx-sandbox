#!/usr/bin/env bash
# ── Auto-deploy the HOSTED-tier sandbox stack on the /srv host ──────────────
#
# Brings the /srv hosted tier in line with matrx-sandbox origin/main:
#   • the `matrx-orchestrator` container (built from orchestrator/)
#   • the spawned-sandbox images matrx-sandbox:{core,slim,aidream}
#
# This is the ONE piece that previously had no pipeline — matrx-sandbox's
# `deploy` job ships the EC2 tier (ECR + SSM) but never touched /srv, so the
# hosted orchestrator and images drifted and had to be hand-built. This script
# closes that gap. It is invoked by .github/workflows/deploy.yml (the
# `deploy-hosted` job, over SSH) and is safe to run by hand or from a poller.
#
# Safety model (matches the operator's rule "keep the old copy if health fails"):
#   • Orchestrator = critical path. Tag the running image :rollback, rebuild,
#     recreate, then poll /health. If it doesn't come healthy, RESTORE the
#     previous image, recreate, and exit non-zero — the last-known-good
#     container keeps serving.
#   • Images = independent + best-effort. Each rebuild tags its own :rollback
#     first and restores it on build failure, so a heavy/broken image build
#     can never take the orchestrator down. A failed image build still marks
#     the run failed (so it's visible) but leaves a working image in place.
#
# Only rebuilds what changed (path diff OLD..NEW), so an orchestrator-only
# commit doesn't trigger a ~5GB aidream rebuild.
#
# Env knobs (all optional):
#   PRE_SYNCED_OLD_SHA  — set by the GHA SSH step: it already did fetch+reset
#                         and captured the pre-reset HEAD here, so we skip git
#                         (avoids a script editing itself mid-run) but still
#                         get an accurate change set.
#   FORCE=1             — rebuild everything regardless of the diff.
#   MATRX_SANDBOX_DIR / ORCH_COMPOSE_DIR / ORCH_HEALTH_URL — path overrides.

set -uo pipefail

REPO_DIR="${MATRX_SANDBOX_DIR:-/srv/projects/matrx-sandbox}"
ORCH_COMPOSE_DIR="${ORCH_COMPOSE_DIR:-/srv/apps/sandbox-orchestrator}"
ORCH_HEALTH_URL="${ORCH_HEALTH_URL:-https://orchestrator.dev.codematrx.com/health}"
ORCH_IMAGE="matrx-orchestrator:latest"

log()  { echo "[deploy-hosted] $*"; }
fail() { echo "[deploy-hosted] ERROR: $*" >&2; exit 1; }

cd "$REPO_DIR" || fail "repo dir $REPO_DIR not found"

# ── Resolve OLD/NEW commit + change set ─────────────────────────────────────
if [ -n "${PRE_SYNCED_OLD_SHA:-}" ]; then
  # GHA already fetched + reset to origin/main and handed us the prior HEAD.
  OLD_SHA="$PRE_SYNCED_OLD_SHA"
  NEW_SHA="$(git rev-parse HEAD)"
else
  OLD_SHA="$(git rev-parse HEAD 2>/dev/null || echo none)"
  git fetch origin main --quiet || fail "git fetch failed"
  NEW_SHA="$(git rev-parse origin/main)"
  git reset --hard origin/main || fail "git reset failed"
fi
log "current=$OLD_SHA target=$NEW_SHA force=${FORCE:-0}"

if [ "${FORCE:-0}" = "1" ] || [ "$OLD_SHA" = "none" ]; then
  CHANGED="ALL"
elif [ "$OLD_SHA" = "$NEW_SHA" ]; then
  log "already at origin/main; nothing to do (FORCE=1 to rebuild anyway)"
  exit 0
else
  CHANGED="$(git diff --name-only "$OLD_SHA" "$NEW_SHA" 2>/dev/null || echo ALL)"
fi
changed() { [ "$CHANGED" = "ALL" ] || grep -q "$1" <<<"$CHANGED"; }

# ── Orchestrator (critical path: health-gated + rollback) ───────────────────
if changed '^orchestrator/'; then
  log "orchestrator/ changed — rebuilding $ORCH_IMAGE"
  docker tag "$ORCH_IMAGE" matrx-orchestrator:rollback 2>/dev/null || true
  if ! docker build -t "$ORCH_IMAGE" "$REPO_DIR/orchestrator"; then
    fail "orchestrator build failed — running container left untouched"
  fi
  ( cd "$ORCH_COMPOSE_DIR" && docker compose up -d --force-recreate ) \
    || fail "orchestrator recreate failed"
  log "waiting for orchestrator /health (up to 60s)…"
  ok=0
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "$ORCH_HEALTH_URL" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if [ "$ok" != 1 ]; then
    log "HEALTH CHECK FAILED — rolling back orchestrator to the previous image"
    if docker image inspect matrx-orchestrator:rollback >/dev/null 2>&1; then
      docker tag matrx-orchestrator:rollback "$ORCH_IMAGE"
      ( cd "$ORCH_COMPOSE_DIR" && docker compose up -d --force-recreate ) || true
      log "rolled back; last-known-good orchestrator is serving"
    fi
    fail "orchestrator unhealthy after rebuild — rolled back"
  fi
  log "orchestrator healthy ✓"
else
  log "orchestrator/ unchanged — skipping orchestrator rebuild"
fi

# ── Sandbox images (independent, best-effort, each rolls back its own tag) ──
IMAGE_FAIL=0
rebuild_image() {
  local tag="$1"; shift
  log "rebuilding $tag"
  docker tag "$tag" "${tag}-rollback" 2>/dev/null || true
  if "$@"; then
    log "$tag rebuilt ✓"
  else
    log "WARNING: $tag build FAILED — restoring previous image"
    docker image inspect "${tag}-rollback" >/dev/null 2>&1 && docker tag "${tag}-rollback" "$tag" || true
    IMAGE_FAIL=1
  fi
}

if changed '^sandbox-image/'; then
  cd "$REPO_DIR/sandbox-image"
  rebuild_image "matrx-sandbox:core" docker build -t matrx-sandbox:core .
  rebuild_image "matrx-sandbox:slim" docker build -t matrx-sandbox:slim -f Dockerfile.slim .
  # aidream is ~5GB and builds ON TOP of :core — only rebuild it when it's
  # already present (don't drag a multi-minute build into every push that
  # touched sandbox-image/; build-aidream.sh requires :core, freshly rebuilt above).
  if docker image inspect matrx-sandbox:aidream >/dev/null 2>&1; then
    rebuild_image "matrx-sandbox:aidream" bash build-aidream.sh
  else
    log "matrx-sandbox:aidream not present — skipping (rebuild on demand from the Manager)"
  fi
else
  log "sandbox-image/ unchanged — skipping image rebuilds"
fi

[ "$IMAGE_FAIL" = 1 ] && fail "one or more sandbox-image rebuilds failed (orchestrator unaffected)"
log "hosted-tier deploy complete at $NEW_SHA"
