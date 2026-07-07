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

# ── Single-flight lock ───────────────────────────────────────────────────────
# Two deploy paths exist (GHA-over-SSH fast path + the local systemd poller,
# scripts/systemd/). Without a lock they can race the same checkout + images
# mid-build. First one wins; the loser exits 0 quietly — the state-file diff
# makes the next poller tick a no-op if the winner already deployed.
LOCK_FILE="${DEPLOY_LOCK_FILE:-/srv/apps/deploy-state/.deploy-hosted.lock}"
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another deploy is already running (lock: $LOCK_FILE) — skipping this run"
  exit 0
fi

# Bring the shared sandbox DB schema forward before the new orchestrator serves
# traffic. Runs inside the orchestrator image (has asyncpg + the migration
# runner + the migrations/ dir baked in) against the orchestrator's own .env.
# Idempotent: already-applied migrations are skipped via the schema_migrations
# ledger, so this is safe to run on every deploy. Fails the deploy on error.
run_db_migrations() {
  log "applying DB migrations (orchestrator.migrate_runner)…"
  if ! docker run --rm --env-file "$ORCH_COMPOSE_DIR/.env" "$ORCH_IMAGE" \
        python -m orchestrator.migrate_runner; then
    fail "DB migrations failed — aborting before recreating orchestrator"
  fi
  log "DB migrations applied ✓"
}

cd "$REPO_DIR" || fail "repo dir $REPO_DIR not found"

# ── Resolve OLD/NEW commit + change set ─────────────────────────────────────
# OLD comes from the last-successful-deploy STATE FILE, not the checkout's
# HEAD. The checkout doubles as a working repo: when an agent commits + pushes
# FROM this server, HEAD already equals origin/main by the time the deploy
# lands, so a HEAD-based diff sees "no changes" and silently deploys nothing
# while the run still goes green. The state file is written only after a
# successful deploy, so a failed run automatically re-diffs from the older
# SHA on the next attempt (self-healing).
STATE_FILE="${DEPLOY_STATE_FILE:-/srv/apps/deploy-state/matrx-sandbox.last-deployed-sha}"
if [ -n "${PRE_SYNCED_OLD_SHA:-}" ]; then
  # GHA already fetched + reset to origin/main and handed us the prior HEAD
  # (fallback only — the state file wins when present).
  OLD_SHA="$(cat "$STATE_FILE" 2>/dev/null || echo "$PRE_SYNCED_OLD_SHA")"
  NEW_SHA="$(git rev-parse HEAD)"
else
  OLD_SHA="$(cat "$STATE_FILE" 2>/dev/null || git rev-parse HEAD 2>/dev/null || echo none)"
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
  # Schema forward using the freshly-built image (carries the latest
  # migrations/) before the new container starts serving.
  run_db_migrations
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

# ── Sandbox images ──────────────────────────────────────────────────────────
# Rebuild an image when EITHER its source changed OR it's missing. The
# "or missing" is the SELF-HEAL: a required image pruned off /srv comes back on
# the next deploy, no matter what the commit touched — so "Missing required
# image: aidream" can't get stuck. Each rebuild is independent + best-effort
# (rolls back its own tag on failure) so a heavy/broken build never takes the
# orchestrator down. A build-status MARKER is written while each image builds so
# the Manager's Fleet Health shows "rebuilding…" instead of a false "missing"
# critical (same dir the Manager's own UI rebuilds use).
BUILD_STATUS_DIR="${IMAGE_BUILD_STATUS_DIR:-/srv/apps/image-build-status}"
mkdir -p "$BUILD_STATUS_DIR" 2>/dev/null || true
IMAGE_FAIL=0
SBX_CHANGED=0;   changed '^sandbox-image/' && SBX_CHANGED=1
LOCAL_CHANGED=0; changed '^sandbox-local/' && LOCAL_CHANGED=1

rebuild_image() {
  local tag="$1"; shift
  local variant="${tag##*:}"
  local marker="$BUILD_STATUS_DIR/${variant}.json"
  log "rebuilding $tag"
  printf '{"variant":"%s","started_at":"%s","source":"deploy-hosted"}\n' \
    "$variant" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$marker" 2>/dev/null || true
  docker tag "$tag" "${tag}-rollback" 2>/dev/null || true
  if "$@"; then
    log "$tag rebuilt ✓"
  else
    log "WARNING: $tag build FAILED — restoring previous image"
    docker image inspect "${tag}-rollback" >/dev/null 2>&1 && docker tag "${tag}-rollback" "$tag" || true
    IMAGE_FAIL=1
  fi
  rm -f "$marker" 2>/dev/null || true
}

# rebuild if sandbox-image/ changed OR the image is absent (self-heal)
need_img()       { [ "$SBX_CHANGED"   = 1 ] || ! docker image inspect "$1" >/dev/null 2>&1; }
# local rebuilds when sandbox-image/ OR sandbox-local/ changes (or the image is missing)
need_local_img() { [ "$SBX_CHANGED"   = 1 ] || [ "$LOCAL_CHANGED" = 1 ] || ! docker image inspect "$1" >/dev/null 2>&1; }

cd "$REPO_DIR/sandbox-image"
# Stamp the zero-drift version (commit SHA) so /etc/sandbox-image-version +
# /drift report the build that produced the image, matching the EC2 job.
# aidream builds FROM matrx-sandbox:core, so it inherits this version file.
if need_img matrx-sandbox:core; then rebuild_image "matrx-sandbox:core" docker build --build-arg MATRX_IMAGE_VERSION="$NEW_SHA" -t matrx-sandbox:core . ; else log "core present + unchanged — skip"; fi
if need_img matrx-sandbox:slim; then rebuild_image "matrx-sandbox:slim" docker build --build-arg MATRX_IMAGE_VERSION="$NEW_SHA" -t matrx-sandbox:slim -f Dockerfile.slim . ; else log "slim present + unchanged — skip"; fi
# aidream is REQUIRED + ~5GB (builds ON TOP of :core, freshly rebuilt above if needed).
# Export MATRX_IMAGE_VERSION so build-aidream.sh forwards it as a build-arg and
# the aidream layer's /etc/sandbox-image-version matches the deploy SHA.
if need_img matrx-sandbox:aidream; then
  rebuild_image "matrx-sandbox:aidream" env MATRX_IMAGE_VERSION="$NEW_SHA" bash build-aidream.sh
else
  log "aidream present + unchanged — skip"
fi

# ── Local starter pool (sandbox-1..5) ───────────────────────────────────────
# The static starter pool predates the dynamic orchestrator and is marked
# deprecated, but it's still serving traffic. Rebuild matrx-sandbox:local +
# recreate the pool when sandbox-image/ OR sandbox-local/ changes (or the image
# is missing) so a push-to-main brings it forward too. The rolling auto-migrate
# loop is orchestrator-driven and does NOT touch these static containers — they
# refresh via this docker-compose recreate. If/when the pool is retired, remove
# this block entirely (don't leave it half-maintained).
if need_local_img matrx-sandbox:local; then
  cd "$REPO_DIR/sandbox-local"
  rebuild_image "matrx-sandbox:local" docker build --build-arg MATRX_IMAGE_VERSION="$NEW_SHA" -t matrx-sandbox:local .
  if [ "$IMAGE_FAIL" != 1 ] && [ -f docker-compose.yml ]; then
    log "recreating local starter pool (sandbox-1..5) on the fresh image"
    docker compose up -d 2>&1 | sed 's/^/[deploy-hosted] starter-pool: /' || \
      log "WARNING: starter pool compose up failed (image is built; pool may be stale)"
  fi
else
  log "local present + unchanged (and sandbox-local/ unchanged) — skip"
fi

[ "$IMAGE_FAIL" = 1 ] && fail "one or more sandbox-image rebuilds failed (orchestrator unaffected)"
# Record the deployed SHA only now — every step above succeeded. This is what
# the next run diffs against (see the state-file comment at the top).
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
echo "$NEW_SHA" > "$STATE_FILE" || log "WARNING: could not write $STATE_FILE"
log "hosted-tier deploy complete at $NEW_SHA"
