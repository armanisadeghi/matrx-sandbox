#!/usr/bin/env bash
# ── Auto-deploy the HOSTED-tier sandbox stack on the /srv host ──────────────
#
# Brings the /srv hosted tier in line with the CI-approved matrx-sandbox SHA:
#   • the `matrx-orchestrator` container (built from orchestrator/)
#   • the spawned-sandbox images matrx-sandbox:{core,slim,aidream}
#
# This is the ONE piece that previously had no pipeline — matrx-sandbox's
# `deploy` job ships the EC2 tier (ECR + SSM) but never touched /srv, so the
# hosted orchestrator and images drifted and had to be hand-built. This script
# closes that gap. It is invoked by .github/workflows/deploy.yml (the
# `deploy-hosted` job, over SSH) and is safe to run by hand or from a poller.
#
# Safety model:
#   • Build every required image under an immutable source-SHA candidate tag.
#   • Verify embedded revisions and run migrations before changing live tags.
#   • Promote the complete candidate set, then assert the exact source/API/FS
#     contract. Any failure or signal restores every prior tag and service.
#
# Only rebuilds what changed (path diff OLD..NEW), so an orchestrator-only
# commit doesn't trigger a ~5GB aidream rebuild.
#
# Env knobs (all optional):
#   DEPLOY_TARGET_SHA   — required full commit SHA from the CI-controlled
#                         deploy/hosted ref. The script never follows main.
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
  local image="$1"
  log "applying DB migrations (orchestrator.migrate_runner)…"
  [ -r "$ORCH_COMPOSE_DIR/.env" ] \
    || fail "required orchestrator env file is unreadable: $ORCH_COMPOSE_DIR/.env"
  grep -q '^MATRX_DATABASE_URL=.' "$ORCH_COMPOSE_DIR/.env" \
    || fail "MATRX_DATABASE_URL is not resolved; refusing to skip required migrations"
  if ! docker run --rm --env-file "$ORCH_COMPOSE_DIR/.env" "$image" \
        python -m orchestrator.migrate_runner; then
    fail "DB migrations failed — aborting before recreating orchestrator"
  fi
  log "DB migrations applied ✓"
}

cd "$REPO_DIR" || fail "repo dir $REPO_DIR not found"

TARGET_SHA="${DEPLOY_TARGET_SHA:-}"
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "DEPLOY_TARGET_SHA must be a full approved commit SHA"
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null)" || fail "cannot resolve checkout HEAD"
[ "$HEAD_SHA" = "$TARGET_SHA" ] || fail "checkout $HEAD_SHA does not match approved target $TARGET_SHA"
git diff --quiet && git diff --cached --quiet \
  || fail "refusing to build a dirty checkout; release images must be reproducible"

# ── aidream image freshness ──────────────────────────────────────────────────
# The aidream variant bakes /srv/projects/aidream's origin/main at build time
# (build-aidream.sh fetches + stamps label com.aimatrx.aidream.sha). It goes
# stale whenever the AIDREAM repo moves — independent of this repo — so the
# check runs on EVERY tick (incl. the "nothing to do" early-exit path below).
# LOUD on lookup failure: a silent empty remote hid a broken git credential
# setup (unit missing HOME) for a day while the check reported "current".
AIDREAM_SRC_DIR="${AIDREAM_SRC_DIR:-/srv/projects/aidream}"
aidream_stale() {
  docker image inspect matrx-sandbox:aidream >/dev/null 2>&1 || return 0   # missing → need_img covers it anyway
  local baked remote
  baked=$(docker image inspect matrx-sandbox:aidream --format '{{index .Config.Labels "com.aimatrx.aidream.sha"}}' 2>/dev/null)
  remote=$(git -C "$AIDREAM_SRC_DIR" ls-remote origin refs/heads/main 2>/dev/null | cut -f1)
  if [ -z "$remote" ]; then
    log "WARNING: aidream freshness UNKNOWN — ls-remote returned nothing (git auth/HOME broken?). Skipping rebuild rather than churning."
    return 1
  fi
  [ -z "$baked" ] && { log "aidream image is unlabeled (pre-freshness build) — rebuilding to stamp it"; return 0; }
  if [ "$baked" != "$remote" ]; then
    log "aidream repo moved: baked ${baked:0:9} → main ${remote:0:9} — aidream image rebuild queued"
    return 0
  fi
  return 1
}

# ── Resolve OLD/NEW commit + change set ─────────────────────────────────────
# OLD comes from the last-successful-deploy STATE FILE, not the checkout's
# HEAD. The checkout doubles as a working repo: when an agent commits + pushes
# FROM this server, HEAD already equals origin/main by the time the deploy
# lands, so a HEAD-based diff sees "no changes" and silently deploys nothing
# while the run still goes green. The state file is written only after a
# successful deploy, so a failed run automatically re-diffs from the older
# SHA on the next attempt (self-healing).
STATE_FILE="${DEPLOY_STATE_FILE:-/srv/apps/deploy-state/matrx-sandbox.last-deployed-sha}"
OLD_SHA="$(cat "$STATE_FILE" 2>/dev/null || echo none)"
NEW_SHA="$TARGET_SHA"
log "current=$OLD_SHA target=$NEW_SHA force=${FORCE:-0}"

if [ "${FORCE:-0}" = "1" ] || [ "$OLD_SHA" = "none" ]; then
  CHANGED="ALL"
elif [ "$OLD_SHA" = "$NEW_SHA" ]; then
  if aidream_stale; then
    # This repo is unchanged but the AIDREAM repo moved — fall through with an
    # empty change set so ONLY the aidream image branch fires below.
    CHANGED=""
  else
    log "approved SHA is already deployed; nothing to do (FORCE=1 to rebuild anyway)"
    exit 0
  fi
else
  CHANGED="$(git diff --name-only "$OLD_SHA" "$NEW_SHA" 2>/dev/null || echo ALL)"
fi
changed() { [ "$CHANGED" = "ALL" ] || grep -q "$1" <<<"$CHANGED"; }

# ── Build orchestrator candidate (no live tags change yet) ──────────────────
ORCH_CHANGED=0
ORCH_CANDIDATE="matrx-orchestrator:sha-$NEW_SHA"
if [ "$OLD_SHA" != "$NEW_SHA" ] || [ "${FORCE:-0}" = 1 ] \
    || ! docker image inspect "$ORCH_IMAGE" >/dev/null 2>&1; then
  ORCH_CHANGED=1
  log "building immutable orchestrator candidate $ORCH_CANDIDATE"
  docker build \
    --build-arg MATRX_SOURCE_SHA="$NEW_SHA" \
    -t "$ORCH_CANDIDATE" "$REPO_DIR/orchestrator" \
    || fail "orchestrator candidate build failed — live deployment untouched"
  baked_source=$(docker image inspect "$ORCH_CANDIDATE" \
    --format '{{index .Config.Labels "com.aimatrx.source.sha"}}')
  [ "$baked_source" = "$NEW_SHA" ] \
    || fail "orchestrator candidate source label mismatch: $baked_source"
else
  log "orchestrator/ unchanged — retaining current orchestrator image"
fi

# ── Sandbox images ──────────────────────────────────────────────────────────
# Build an immutable candidate when EITHER its source changed OR it's missing. The
# "or missing" is the SELF-HEAL: a required image pruned off /srv comes back on
# the next deploy, no matter what the commit touched — so "Missing required
# image: aidream" can't get stuck. No live tag changes until every required
# candidate has built and its embedded source SHA is verified. A build-status
# MARKER is written while each image builds so
# the Manager's Fleet Health shows "rebuilding…" instead of a false "missing"
# critical (same dir the Manager's own UI rebuilds use).
BUILD_STATUS_DIR="${IMAGE_BUILD_STATUS_DIR:-/srv/apps/image-build-status}"
mkdir -p "$BUILD_STATUS_DIR" 2>/dev/null || true
SBX_CHANGED=0;   changed '^sandbox-image/' && SBX_CHANGED=1
LOCAL_CHANGED=0; changed '^sandbox-local/' && LOCAL_CHANGED=1
LIVE_TAGS=()
CANDIDATE_TAGS=()

build_candidate() {
  local live_tag="$1" candidate_tag="$2"; shift 2
  local variant="${live_tag##*:}"
  local marker="$BUILD_STATUS_DIR/${variant}.json"
  log "building immutable candidate $candidate_tag"
  printf '{"variant":"%s","started_at":"%s","source":"deploy-hosted"}\n' \
    "$variant" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$marker" 2>/dev/null || true
  "$@" || { rm -f "$marker"; fail "$candidate_tag build failed — live release untouched"; }
  rm -f "$marker" 2>/dev/null || true
  local baked
  baked=$(docker image inspect "$candidate_tag" \
    --format '{{index .Config.Labels "com.aimatrx.sandbox.version"}}')
  [ "$baked" = "$NEW_SHA" ] \
    || fail "$candidate_tag version mismatch: expected $NEW_SHA, found $baked"
  LIVE_TAGS+=("$live_tag")
  CANDIDATE_TAGS+=("$candidate_tag")
}

# rebuild if sandbox-image/ changed OR the image is absent (self-heal)
need_img()       { [ "$SBX_CHANGED"   = 1 ] || ! docker image inspect "$1" >/dev/null 2>&1; }

# local rebuilds when sandbox-image/ OR sandbox-local/ changes (or the image is missing)
need_local_img() { [ "$SBX_CHANGED"   = 1 ] || [ "$LOCAL_CHANGED" = 1 ] || ! docker image inspect "$1" >/dev/null 2>&1; }

cd "$REPO_DIR/sandbox-image"
# Stamp the zero-drift version (commit SHA) so /etc/sandbox-image-version +
# /drift report the build that produced the image, matching the EC2 job.
# aidream builds FROM matrx-sandbox:core, so it inherits this version file.
CORE_BUILD_VERSION=core
if need_img matrx-sandbox:core; then
  CORE_CANDIDATE="matrx-sandbox:core-$NEW_SHA"
  build_candidate matrx-sandbox:core "$CORE_CANDIDATE" \
    docker build --build-arg MATRX_IMAGE_VERSION="$NEW_SHA" -t "$CORE_CANDIDATE" .
  CORE_BUILD_VERSION="core-$NEW_SHA"
else
  log "core present + unchanged — skip"
fi
if need_img matrx-sandbox:slim; then
  SLIM_CANDIDATE="matrx-sandbox:slim-$NEW_SHA"
  build_candidate matrx-sandbox:slim "$SLIM_CANDIDATE" \
    docker build --build-arg MATRX_IMAGE_VERSION="$NEW_SHA" -t "$SLIM_CANDIDATE" -f Dockerfile.slim .
else
  log "slim present + unchanged — skip"
fi
# aidream is REQUIRED + ~5GB (builds ON TOP of :core, freshly rebuilt above if needed).
# Export MATRX_IMAGE_VERSION so build-aidream.sh forwards it as a build-arg and
# the aidream layer's /etc/sandbox-image-version matches the deploy SHA.
if need_img matrx-sandbox:aidream || aidream_stale; then
  AIDREAM_SOURCE_SHA=$(git -C "$AIDREAM_SRC_DIR" ls-remote origin refs/heads/main 2>/dev/null | cut -f1)
  [[ "$AIDREAM_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || fail "cannot resolve immutable aidream source SHA"
  AIDREAM_CANDIDATE="matrx-sandbox:aidream-$NEW_SHA-${AIDREAM_SOURCE_SHA:0:12}"
  build_candidate matrx-sandbox:aidream "$AIDREAM_CANDIDATE" \
    env MATRX_IMAGE_VERSION="$NEW_SHA" MATRX_CORE_VERSION="$CORE_BUILD_VERSION" \
      bash build-aidream.sh --tag "$AIDREAM_CANDIDATE"
  baked_aidream=$(docker image inspect "$AIDREAM_CANDIDATE" \
    --format '{{index .Config.Labels "com.aimatrx.aidream.sha"}}')
  [ "$baked_aidream" = "$AIDREAM_SOURCE_SHA" ] \
    || fail "$AIDREAM_CANDIDATE aidream source mismatch: $baked_aidream"
else
  log "aidream present + unchanged (aidream repo SHA current) — skip"
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
  LOCAL_CANDIDATE="matrx-sandbox:local-$NEW_SHA"
  build_candidate matrx-sandbox:local "$LOCAL_CANDIDATE" \
    docker build --build-arg CORE_VERSION="$CORE_BUILD_VERSION" \
      --build-arg MATRX_IMAGE_VERSION="$NEW_SHA" -t "$LOCAL_CANDIDATE" .
else
  log "local present + unchanged (and sandbox-local/ unchanged) — skip"
fi

# ── Promote the complete candidate set as one rollback-capable release ──────
if [ "$ORCH_CHANGED" = 1 ]; then
  run_db_migrations "$ORCH_CANDIDATE"
fi

rollback_release() {
  log "rolling back all live tags from the failed release"
  local index live
  for live in "${PROMOTED_TAGS[@]}"; do
    if docker image inspect "${live}-rollback" >/dev/null 2>&1; then
      docker tag "${live}-rollback" "$live"
    else
      docker image rm "$live" >/dev/null 2>&1 || true
    fi
  done
  if [ "$ORCH_CHANGED" = 1 ]; then
    if docker image inspect matrx-orchestrator:rollback >/dev/null 2>&1; then
      docker tag matrx-orchestrator:rollback "$ORCH_IMAGE"
      ( cd "$ORCH_COMPOSE_DIR" && docker compose up -d --force-recreate ) || true
    else
      docker image rm "$ORCH_IMAGE" >/dev/null 2>&1 || true
    fi
  fi
  if [ -n "${LOCAL_CANDIDATE:-}" ] && [ -f "$REPO_DIR/sandbox-local/docker-compose.yml" ]; then
    ( cd "$REPO_DIR/sandbox-local" && docker compose up -d ) || true
  fi
}
fail_release() { PROMOTION_ACTIVE=0; rollback_release; fail "$*"; }

PROMOTED_TAGS=()
PROMOTION_ACTIVE=1
trap 'status=$?; trap - EXIT INT TERM; if [ "${PROMOTION_ACTIVE:-0}" = 1 ]; then PROMOTION_ACTIVE=0; rollback_release; fi; exit $status' EXIT INT TERM
for index in "${!LIVE_TAGS[@]}"; do
  live="${LIVE_TAGS[$index]}"
  candidate="${CANDIDATE_TAGS[$index]}"
  if docker image inspect "$live" >/dev/null 2>&1; then
    docker tag "$live" "${live}-rollback"
  else
    docker image rm "${live}-rollback" >/dev/null 2>&1 || true
  fi
  docker tag "$candidate" "$live" || fail_release "could not promote $candidate"
  PROMOTED_TAGS+=("$live")
done

if [ "$ORCH_CHANGED" = 1 ]; then
  if docker image inspect "$ORCH_IMAGE" >/dev/null 2>&1; then
    docker tag "$ORCH_IMAGE" matrx-orchestrator:rollback
  else
    docker image rm matrx-orchestrator:rollback >/dev/null 2>&1 || true
  fi
  docker tag "$ORCH_CANDIDATE" "$ORCH_IMAGE" \
    || fail_release "could not promote $ORCH_CANDIDATE"
  ( cd "$ORCH_COMPOSE_DIR" && docker compose up -d --force-recreate ) \
    || fail_release "orchestrator recreate failed"
fi

if [ -n "${LOCAL_CANDIDATE:-}" ] && [ -f "$REPO_DIR/sandbox-local/docker-compose.yml" ]; then
  log "recreating local starter pool (sandbox-1..5) on the promoted image"
  ( cd "$REPO_DIR/sandbox-local" && docker compose up -d ) \
    || fail_release "starter pool recreate failed"
fi

log "waiting for orchestrator release contract (up to 60s)…"
ORCH_API_KEY=$(grep '^MATRX_API_KEY=' "$ORCH_COMPOSE_DIR/.env" | head -1 | cut -d= -f2-)
[ -n "$ORCH_API_KEY" ] || fail_release "MATRX_API_KEY is not resolved for post-deploy verification"
verified=0
for _ in $(seq 1 30); do
  if payload=$(curl -fsS --max-time 5 -H "X-API-Key: $ORCH_API_KEY" \
      "${ORCH_HEALTH_URL%/health}/api-surface" 2>/dev/null) \
      && RELEASE_PAYLOAD="$payload" EXPECTED_SHA="$NEW_SHA" python3 - <<'PY'
import json, os
d = json.loads(os.environ["RELEASE_PAYLOAD"])
paths = {r["path"] for r in d.get("routes", [])}
required = {"/sandboxes/{sandbox_id}/fs/{path:path}", "/sandboxes/{sandbox_id}/fs/watch"}
assert d.get("source_sha") == os.environ["EXPECTED_SHA"]
assert d.get("contracts", {}).get("filesystem") == 2
assert required <= paths
PY
  then verified=1; break; fi
  sleep 2
done
[ "$verified" = 1 ] || fail_release "exact source/API/filesystem contract verification failed"
log "release contract verified at $NEW_SHA ✓"

# Refresh the out-of-checkout poller only after this release is healthy. The
# next timer tick will therefore continue following the CI-approved ref.
install -m 0755 "$REPO_DIR/scripts/systemd/pull-deploy-runner.sh" \
  /usr/local/bin/matrx-hosted-deploy-runner \
  || fail_release "could not install approved-ref poller"

# ── GC: keep frequent image rebuilds from eating the disk ────────────────────
# The aidream variant rebuilds on every aidream push (several times/day), each
# leaving GBs of dangling layers. Prune ONLY dangling images (untagged — never
# touches :latest/:rollback/anything named) + cap build cache. Best-effort:
# GC failure must never fail a deploy.
reclaimed=$(docker image prune -f 2>/dev/null | grep -oE "reclaimed space: .*" || true)
docker builder prune -f --keep-storage=20GB >/dev/null 2>&1 || true
log "image GC: ${reclaimed:-nothing dangling}; build cache capped at 20GB; disk free: $(df -h / | awk 'NR==2{print $4}')"

# Keep the current immutable candidates plus the live/rollback tags; remove
# older candidate *tags* so repeated releases cannot consume the host disk.
while IFS= read -r old_candidate; do
  if [[ "$old_candidate" =~ ^matrx-orchestrator:sha-[0-9a-f]{40}$ \
        || "$old_candidate" =~ ^matrx-sandbox:(core|slim|local)-[0-9a-f]{40}$ \
        || "$old_candidate" =~ ^matrx-sandbox:aidream-[0-9a-f]{40}-[0-9a-f]{12}$ ]]; then
    [[ "$old_candidate" == *"$NEW_SHA"* ]] \
      || docker image rm "$old_candidate" >/dev/null 2>&1 || true
  fi
done < <(docker image ls --format '{{.Repository}}:{{.Tag}}')

# Record the deployed SHA only now — every step above succeeded. This is what
# the next run diffs against (see the state-file comment at the top).
mkdir -p "$(dirname "$STATE_FILE")" || fail_release "could not create deploy state directory"
STATE_TMP="${STATE_FILE}.tmp.$$"
printf '%s\n' "$NEW_SHA" > "$STATE_TMP" \
  && mv -f "$STATE_TMP" "$STATE_FILE" \
  || fail_release "could not atomically record deployed SHA"
PROMOTION_ACTIVE=0
trap - EXIT INT TERM
log "hosted-tier deploy complete at $NEW_SHA"
