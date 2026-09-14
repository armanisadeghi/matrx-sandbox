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
#   DEPLOY_TARGET_SHA   — full commit SHA from the CI-controlled deploy/hosted
#                         ref. If omitted by the legacy poller during rollout,
#                         the script resolves that ref itself and still fails
#                         closed unless the checkout exactly matches it.
#   FORCE=1             — rebuild everything regardless of the diff.
#   MATRX_SANDBOX_DIR / ORCH_COMPOSE_DIR / ORCH_HEALTH_URL — path overrides.
#   ORCH_STARTUP_TIMEOUT_SECONDS — startup verification budget, 30..1800 (300 default).
#   AIDREAM_REBUILD_MIN_INTERVAL_SECONDS — floor between aidream TEMPLATE image
#                         rebuilds, seconds (21600 = 6 h default; 0 disables the
#                         floor). FORCE=1 always ignores it.
#   AIDREAM_REBUILD_STAMP — path of the epoch stamp that floor reads/writes.
#   DEPLOY_LOCK_WAIT_SECONDS — bounded wait for the shared `deployment` lease on
#                         the safe promotion path, 0..3600 (120 default;
#                         0 = a single non-blocking attempt).

set -uo pipefail

REPO_DIR="${MATRX_SANDBOX_DIR:-/srv/projects/matrx-sandbox}"
ORCH_COMPOSE_DIR="${ORCH_COMPOSE_DIR:-/srv/apps/sandbox-orchestrator}"
ORCH_HEALTH_URL="${ORCH_HEALTH_URL:-https://orchestrator.dev.codematrx.com/health}"
HOSTED_MIGRATION_STATE_DIR="$ORCH_COMPOSE_DIR/hosted-migrations"
ORCH_IMAGE="matrx-orchestrator:latest"
MAX_IMAGE_AGE_SECONDS="${MAX_IMAGE_AGE_SECONDS:-1209600}" # 14 days; matches Fleet Health
# ── The aidream TEMPLATE rebuild floor (a knob, not a constant) ─────────────
# aidream's own main branch moves many times an hour (its deploy train pushes
# every ~20-30 min, plus agent commits). `aidream_stale` compares the baked
# label to that remote head, so before this floor existed EVERY 2-minute poller
# tick that saw a new aidream commit started a ~6 GB, ~3-minute, heavy-I/O
# template build — ~40 of them in 12 h on 2026-09-14 — and each one that
# finished took the release promotion barrier, which stops and recreates the
# single-replica live orchestrator behind a health-gated router. Sustained
# build I/O is exactly what removed that orchestrator from the edge on
# 2026-09-13 (docs/incidents/2026-09-13-edge-drop-reconcile.md).
# Per-commit currency buys a development template nothing, so the cadence is an
# operator knob with an agent-chosen default. Untouched by this floor: a
# missing image, MAX_IMAGE_AGE_SECONDS freshness, a matrx-sandbox source change,
# and FORCE=1.
AIDREAM_REBUILD_MIN_INTERVAL_SECONDS="${AIDREAM_REBUILD_MIN_INTERVAL_SECONDS:-21600}" # 6 h
AIDREAM_REBUILD_STAMP="${AIDREAM_REBUILD_STAMP:-/srv/apps/deploy-state/matrx-sandbox.aidream-build-epoch}"
ORCH_STARTUP_TIMEOUT_SECONDS="${ORCH_STARTUP_TIMEOUT_SECONDS:-300}"
# ── The promotion lock wait (a knob, not a constant) ────────────────────────
# The shared `deployment` lease is also taken, briefly, by the 60-second
# liveness reconcile sweep and by every admitted sandbox migration. A single
# non-blocking attempt therefore lost roughly two ticks in five to a sweep that
# was already finishing, and each loss rolled a fully built, fully verified
# candidate back and waited for the next poller tick — the deadlock class this
# knob closes. A deploy is not latency-sensitive; a bounded wait is.
DEPLOY_LOCK_WAIT_SECONDS="${DEPLOY_LOCK_WAIT_SECONDS:-120}"

# Why a failure file: Fleet Health could see the poller was stuck but not WHY,
# so its only advice was "ssh in and read journalctl". A 20 h wedge on
# 2026-08-11 went undiagnosed that way. Every fail() now records the reason
# beside the deploy state, where the Manager reads it into the dashboard; a
# successful release clears it.
FAILURE_FILE="${DEPLOY_FAILURE_FILE:-/srv/apps/deploy-state/matrx-sandbox.last-failure.json}"
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/ /g'; }
record_failure() {
  mkdir -p "$(dirname "$FAILURE_FILE")" 2>/dev/null || true
  printf '{"sha":"%s","at":"%s","reason":"%s"}\n' \
    "$(json_escape "${TARGET_SHA:-unknown}")" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$(json_escape "$1")" > "$FAILURE_FILE" 2>/dev/null || true
}
clear_failure() { rm -f "$FAILURE_FILE" 2>/dev/null || true; }

log()  { echo "[deploy-hosted] $*"; }
fail() { echo "[deploy-hosted] ERROR: $*" >&2; record_failure "$*"; exit 1; }
[[ "$ORCH_STARTUP_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{1,3}$ ]] \
  && (( ORCH_STARTUP_TIMEOUT_SECONDS >= 30 && ORCH_STARTUP_TIMEOUT_SECONDS <= 1800 )) \
  || fail "ORCH_STARTUP_TIMEOUT_SECONDS must be an integer from 30 through 1800"
[[ "$AIDREAM_REBUILD_MIN_INTERVAL_SECONDS" =~ ^(0|[1-9][0-9]{0,7})$ ]] \
  || fail "AIDREAM_REBUILD_MIN_INTERVAL_SECONDS must be a whole number of seconds (0 disables the floor)"
[[ "$DEPLOY_LOCK_WAIT_SECONDS" =~ ^(0|[1-9][0-9]{0,3})$ ]] \
  && (( DEPLOY_LOCK_WAIT_SECONDS <= 3600 )) \
  || fail "DEPLOY_LOCK_WAIT_SECONDS must be a whole number of seconds from 0 through 3600 (0 = one non-blocking attempt)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/release-guard.sh
source "$SCRIPT_DIR/lib/release-guard.sh" \
  || fail "cannot load release ancestry guard"

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
  # Pre-flight for the store guard in orchestrator/config.py: the new container
  # refuses to boot unless this is 'postgres'. Catch it before the swap.
  grep -q '^MATRX_SANDBOX_STORE=postgres[[:space:]]*$' "$ORCH_COMPOSE_DIR/.env" \
    || fail "MATRX_SANDBOX_STORE must be 'postgres' in $ORCH_COMPOSE_DIR/.env (in-memory loses every sandbox row on restart)"
  grep -q '^MATRX_HOST_TIER=hosted[[:space:]]*$' "$ORCH_COMPOSE_DIR/.env" \
    || fail "MATRX_HOST_TIER must be 'hosted' in $ORCH_COMPOSE_DIR/.env (token issuance and lifecycle routing require exact tier identity)"
  prepare_hosted_journal "$image"
  if ! docker run --rm --env-file "$ORCH_COMPOSE_DIR/.env" "$image" \
        python -m orchestrator.migrate_runner; then
    fail "DB migrations failed — aborting before recreating orchestrator"
  fi
  log "DB migrations applied ✓"
}

prepare_hosted_journal() {
  local image="$1" source override state
  source="$REPO_DIR/infra/hosted/docker-compose.override.yml"
  override="$ORCH_COMPOSE_DIR/docker-compose.override.yml"
  state="${HOSTED_MIGRATION_STATE_DIR:-$ORCH_COMPOSE_DIR/hosted-migrations}"
  [ -f "$source" ] || fail "canonical hosted journal compose overlay is missing"
  # Never overwrite an operator's unrelated override or follow a substituted
  # state/override symlink. An existing journal's permissions are evidence to
  # validate, not something a deploy should silently repair.
  [ ! -L "$override" ] && [ ! -L "$state" ] \
    || fail "hosted journal override/state must not be symbolic links"
  if [ -e "$override" ]; then
    cmp -s "$source" "$override" \
      || fail "hosted compose override differs from canonical journal overlay; reconcile before deploy"
  fi
  if [ ! -e "$state" ]; then
    install -d -m 0700 "$state" || fail "cannot provision durable hosted journal directory"
  fi
  # Run as the candidate image's actual USER against the actual mount. This
  # validates ownership, mode, mount, writes and fsync before any service swap.
  docker run --rm --network none --entrypoint python \
    --mount "type=bind,src=$state,dst=/var/lib/matrx-sandbox/hosted-migrations" \
    "$image" -c 'from orchestrator.hosted_migration import HostedMigrationJournal; HostedMigrationJournal().ensure_ready()' \
    || fail "candidate cannot safely use durable hosted migration journal"
  # Only add the mount to subsequent Manager recreates after its real directory
  # is ready; a failed preflight leaves the previous compose configuration intact.
  if [ ! -e "$override" ]; then
    install -m 0644 "$source" "$override" \
      || fail "cannot install hosted journal compose overlay"
  fi
  ( cd "$ORCH_COMPOSE_DIR" && docker compose config --format json ) \
    | python3 -c 'import json,sys; c=json.load(sys.stdin); v=c["services"]["orchestrator"].get("volumes",[]); p="/var/lib/matrx-sandbox/hosted-migrations"; matches=[m for m in v if m.get("target")==p]; sys.exit(0 if len(matches)==1 and matches[0].get("type")=="bind" and not matches[0].get("read_only",False) and matches[0].get("source")==sys.argv[1] else 1)' "$state" \
    || fail "effective hosted compose does not retain the verified journal mount"
}

cd "$REPO_DIR" || fail "repo dir $REPO_DIR not found"

HEAD_SHA="$(git rev-parse HEAD 2>/dev/null)" || fail "cannot resolve checkout HEAD"
TARGET_SHA="${DEPLOY_TARGET_SHA:-}"
if [ -z "$TARGET_SHA" ]; then
  git fetch origin refs/heads/deploy/hosted --quiet \
    || fail "cannot resolve CI-approved deploy/hosted ref"
  TARGET_SHA="$(git rev-parse FETCH_HEAD)"
fi
release_guard_validate_sha "$TARGET_SHA" "approved target"
[ "$HEAD_SHA" = "$TARGET_SHA" ] || fail "checkout $HEAD_SHA does not match approved target $TARGET_SHA"
git diff --quiet && git diff --cached --quiet \
  || fail "refusing to build a dirty checkout; release images must be reproducible"

# Database migrations only move forward. Reject stale workflow reruns and any
# target that does not descend from every locally observable deployed revision
# before a candidate can run migrations.
STATE_FILE="${DEPLOY_STATE_FILE:-/srv/apps/deploy-state/matrx-sandbox.last-deployed-sha}"
IMAGE_STATE_FILE="${DEPLOY_IMAGE_STATE_FILE:-${STATE_FILE}.images}"
OLD_SHA="$(cat "$STATE_FILE" 2>/dev/null || echo none)"
validate_release_authority() {
  release_guard_fetch_approved_release "$REPO_DIR" "$TARGET_SHA"
  if [ "$OLD_SHA" != "none" ]; then
    release_guard_assert_descendant \
      "$REPO_DIR" "$OLD_SHA" "$TARGET_SHA" "hosted deploy state"
  fi
  if docker image inspect "$ORCH_IMAGE" >/dev/null 2>&1; then
    LIVE_SOURCE=$(docker image inspect "$ORCH_IMAGE" \
      --format '{{index .Config.Labels "com.aimatrx.source.sha"}}' 2>/dev/null)
    if [[ "$LIVE_SOURCE" =~ ^[0-9a-f]{40}$ ]]; then
      release_guard_assert_descendant \
        "$REPO_DIR" "$LIVE_SOURCE" "$TARGET_SHA" "live hosted orchestrator"
    elif [ "$OLD_SHA" = "none" ]; then
      fail "live hosted orchestrator is unversioned and no deploy state exists"
    else
      log "live hosted orchestrator label is missing/invalid — state ancestry passed; self-heal required"
    fi
  fi
}
validate_release_authority

# ── aidream image freshness ──────────────────────────────────────────────────
# The aidream variant bakes /srv/projects/aidream's origin/main at build time
# (build-aidream.sh fetches + stamps label com.aimatrx.aidream.sha). It goes
# stale whenever the AIDREAM repo moves — independent of this repo — so the
# check runs on EVERY tick (incl. the "nothing to do" early-exit path below).
# LOUD on lookup failure: a silent empty remote hid a broken git credential
# setup (unit missing HOME) for a day while the check reported "current".
AIDREAM_SRC_DIR="${AIDREAM_SRC_DIR:-/srv/projects/aidream}"
resolve_aidream_source_sha() {
  local remote
  remote=$(git -C "$AIDREAM_SRC_DIR" ls-remote origin refs/heads/main 2>/dev/null | cut -f1)
  if [[ "$remote" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' "$remote"
    return 0
  fi
  return 1
}
aidream_stale() {
  docker image inspect matrx-sandbox:aidream >/dev/null 2>&1 || return 0   # missing → need_img covers it anyway
  local baked remote
  baked=$(docker image inspect matrx-sandbox:aidream --format '{{index .Config.Labels "com.aimatrx.aidream.sha"}}' 2>/dev/null)
  remote=$(resolve_aidream_source_sha || true)
  if [ -z "$remote" ]; then
    log "ERROR: aidream freshness UNKNOWN — authoritative origin/main lookup failed; refusing a stale tracking-ref fallback"
    return 0
  fi
  [ -z "$baked" ] && { log "aidream image is unlabeled (pre-freshness build) — rebuilding to stamp it"; return 0; }
  if [ "$baked" != "$remote" ]; then
    local since
    if since=$(aidream_rebuild_floor_remaining); then
      log "aidream repo moved: baked ${baked:0:9} → main ${remote:0:9} — rebuild DEFERRED: the last aidream template build started ${since}s ago, under the ${AIDREAM_REBUILD_MIN_INTERVAL_SECONDS}s AIDREAM_REBUILD_MIN_INTERVAL_SECONDS floor. Rebuild now with FORCE=1, or lower/zero the knob."
      return 1
    fi
    log "aidream repo moved: baked ${baked:0:9} → main ${remote:0:9} — aidream image rebuild queued"
    return 0
  fi
  return 1
}

# Succeeds (and prints the age of the last build attempt) ONLY while the floor
# is still holding a rebuild back. Fails open: no knob, no stamp, an unreadable
# or corrupt stamp, or a clock that went backwards all mean "rebuild allowed".
aidream_rebuild_floor_remaining() {
  local last now age
  [ "$AIDREAM_REBUILD_MIN_INTERVAL_SECONDS" -gt 0 ] || return 1
  last=$(cat "$AIDREAM_REBUILD_STAMP" 2>/dev/null) || return 1
  [[ "$last" =~ ^[0-9]+$ ]] || return 1
  now=$(date -u +%s)
  age=$((now - last))
  [ "$age" -ge 0 ] || return 1
  [ "$age" -lt "$AIDREAM_REBUILD_MIN_INTERVAL_SECONDS" ] || return 1
  printf '%s\n' "$age"
}

# Stamped when a build STARTS, never when it finishes: a template build that
# fails must not re-fire every 2 minutes, which is the same treadmill.
stamp_aidream_rebuild() {
  mkdir -p "$(dirname "$AIDREAM_REBUILD_STAMP")" 2>/dev/null \
    || { log "WARNING: cannot create $(dirname "$AIDREAM_REBUILD_STAMP") — the aidream rebuild floor will not hold"; return 0; }
  date -u +%s > "$AIDREAM_REBUILD_STAMP" 2>/dev/null \
    || log "WARNING: cannot write $AIDREAM_REBUILD_STAMP — the aidream rebuild floor will not hold"
}

# ── Resolve OLD/NEW commit + change set ─────────────────────────────────────
# OLD comes from the last-successful-deploy STATE FILE, not the checkout's
# HEAD. The checkout doubles as a working repo: when an agent commits + pushes
# FROM this server, HEAD already equals origin/main by the time the deploy
# lands, so a HEAD-based diff sees "no changes" and silently deploys nothing
# while the run still goes green. The state file is written only after a
# successful deploy, so a failed run automatically re-diffs from the older
# SHA on the next attempt (self-healing).
NEW_SHA="$TARGET_SHA"
log "current=$OLD_SHA target=$NEW_SHA force=${FORCE:-0}"

image_label_matches() {
  local image="$1" label="$2" expected="$3" actual
  docker image inspect "$image" >/dev/null 2>&1 || return 1
  actual=$(docker image inspect "$image" \
    --format "{{index .Config.Labels \"$label\"}}" 2>/dev/null) || return 1
  [ "$actual" = "$expected" ]
}

image_version_compatible() {
  local image="$1" actual
  docker image inspect "$image" >/dev/null 2>&1 || return 1
  actual=$(docker image inspect "$image" \
    --format '{{index .Config.Labels "com.aimatrx.sandbox.version"}}' 2>/dev/null) \
    || return 1
  [[ "$actual" =~ ^[0-9a-f]{40}$ ]] || return 1
  git merge-base --is-ancestor "$actual" "$NEW_SHA" 2>/dev/null
}

image_state_matches() {
  local image="$1" expected actual
  [ -r "$IMAGE_STATE_FILE" ] || return 1
  expected=$(awk -F '\t' -v image="$image" '$1 == image {print $2; found++} END {if (found != 1) exit 1}' \
    "$IMAGE_STATE_FILE") || return 1
  actual=$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null) || return 1
  [ "$actual" = "$expected" ]
}

image_too_old() {
  local image="$1" created created_epoch now age
  docker image inspect "$image" >/dev/null 2>&1 || return 0
  created=$(docker image inspect "$image" --format '{{.Created}}' 2>/dev/null) || return 1
  created_epoch=$(date -u -d "$created" +%s 2>/dev/null) || {
    log "WARNING: cannot parse creation time for $image; freshness is unknown"
    return 1
  }
  now=$(date -u +%s)
  age=$((now - created_epoch))
  if [ "$age" -ge "$MAX_IMAGE_AGE_SECONDS" ]; then
    log "$image is ${age}s old (limit ${MAX_IMAGE_AGE_SECONDS}s) — freshness rebuild queued"
    return 0
  fi
  return 1
}

hosted_release_complete() {
  image_label_matches "$ORCH_IMAGE" com.aimatrx.source.sha "$NEW_SHA" \
    && image_state_matches "$ORCH_IMAGE" \
    && image_version_compatible matrx-sandbox:core \
    && image_state_matches matrx-sandbox:core \
    && ! image_too_old matrx-sandbox:core \
    && image_version_compatible matrx-sandbox:slim \
    && image_state_matches matrx-sandbox:slim \
    && ! image_too_old matrx-sandbox:slim \
    && image_version_compatible matrx-sandbox:aidream \
    && image_state_matches matrx-sandbox:aidream \
    && ! image_too_old matrx-sandbox:aidream \
    && image_version_compatible matrx-sandbox:local \
    && image_state_matches matrx-sandbox:local \
    && ! image_too_old matrx-sandbox:local
}

SELF_HEAL=0
IMAGE_STATE_PRESENT=0
[ -r "$IMAGE_STATE_FILE" ] && IMAGE_STATE_PRESENT=1
if [ "${FORCE:-0}" = "1" ] || [ "$OLD_SHA" = "none" ]; then
  CHANGED="ALL"
elif [ "$OLD_SHA" = "$NEW_SHA" ]; then
  if ! hosted_release_complete; then
    log "deployed SHA is current but required live aliases are missing or stale — self-heal queued"
    SELF_HEAL=1
    CHANGED=""
  elif aidream_stale; then
    # This repo is unchanged but the AIDREAM repo moved — fall through with an
    # empty change set so ONLY the aidream image branch fires below.
    CHANGED=""
  else
    log "approved SHA is already deployed; nothing to do (FORCE=1 to rebuild anyway)"
    exit 0
  fi
else
  CHANGED="$(git diff --name-only "$OLD_SHA" "$NEW_SHA")" \
    || fail "cannot compute the validated release change set"
fi
changed() { [ "$CHANGED" = "ALL" ] || grep -q "$1" <<<"$CHANGED"; }

# ── Build orchestrator candidate (no live tags change yet) ──────────────────
ORCH_CHANGED=0
ORCH_CANDIDATE="matrx-orchestrator:sha-$NEW_SHA"
BUILD_STATUS_DIR="${IMAGE_BUILD_STATUS_DIR:-/srv/apps/image-build-status}"
mkdir -p "$BUILD_STATUS_DIR" 2>/dev/null || true
BUILD_MARKERS=()

mark_build_pending() {
  local variant="$1"
  local marker="$BUILD_STATUS_DIR/${variant}.json"
  printf '{"variant":"%s","started_at":"%s","source":"deploy-hosted"}\n' \
    "$variant" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$marker" 2>/dev/null || true
  BUILD_MARKERS+=("$marker")
}

clear_build_marker() {
  local variant="$1"
  rm -f "$BUILD_STATUS_DIR/${variant}.json" 2>/dev/null || true
}

clear_all_build_markers() {
  local marker
  for marker in "${BUILD_MARKERS[@]}"; do
    rm -f "$marker" 2>/dev/null || true
  done
}

# A marker covers the complete build -> promotion window, not merely the
# docker build subprocess. Otherwise Fleet Health reports a candidate awaiting
# atomic promotion as a permanent missing-image incident. Any failure clears
# every marker so a dead deploy never looks alive for the 30-minute UI TTL.
trap 'status=$?; clear_all_build_markers; exit $status' EXIT INT TERM

if [ "$OLD_SHA" != "$NEW_SHA" ] || [ "${FORCE:-0}" = 1 ] \
    || ! image_label_matches "$ORCH_IMAGE" com.aimatrx.source.sha "$NEW_SHA" \
    || { [ "$SELF_HEAL" = 1 ] && [ "$IMAGE_STATE_PRESENT" = 1 ] \
         && ! image_state_matches "$ORCH_IMAGE"; }; then
  ORCH_CHANGED=1
  log "building immutable orchestrator candidate $ORCH_CANDIDATE"
  mark_build_pending orchestrator
  docker build \
    --build-arg MATRX_SOURCE_SHA="$NEW_SHA" \
    -t "$ORCH_CANDIDATE" "$REPO_DIR/orchestrator" \
    || { clear_build_marker orchestrator; fail "orchestrator candidate build failed — live deployment untouched"; }
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
SBX_CHANGED=0;   changed '^sandbox-image/' && SBX_CHANGED=1
LOCAL_CHANGED=0; changed '^sandbox-local/' && LOCAL_CHANGED=1
LIVE_TAGS=()
CANDIDATE_TAGS=()

build_candidate() {
  local live_tag="$1" candidate_tag="$2"; shift 2
  local variant="${live_tag##*:}"
  log "building immutable candidate $candidate_tag"
  mark_build_pending "$variant"
  "$@" || { clear_build_marker "$variant"; fail "$candidate_tag build failed — live release untouched"; }
  local baked
  baked=$(docker image inspect "$candidate_tag" \
    --format '{{index .Config.Labels "com.aimatrx.sandbox.version"}}')
  [ "$baked" = "$NEW_SHA" ] \
    || fail "$candidate_tag version mismatch: expected $NEW_SHA, found $baked"
  LIVE_TAGS+=("$live_tag")
  CANDIDATE_TAGS+=("$candidate_tag")
}

# Rebuild if sandbox-image/ changed or a required live alias is absent/stale.
need_img() {
  [ "$SBX_CHANGED" = 1 ] \
    || ! image_version_compatible "$1" \
    || image_too_old "$1" \
    || { [ "$SELF_HEAL" = 1 ] && [ "$IMAGE_STATE_PRESENT" = 1 ] \
         && ! image_state_matches "$1"; }
}

# Local also inherits core, so a core self-heal invalidates its base even when
# the existing local alias happens to carry the expected version label.
need_local_img() {
  [ "$SBX_CHANGED" = 1 ] || [ "$LOCAL_CHANGED" = 1 ] \
    || [ "${CORE_REBUILT:-0}" = 1 ] \
    || ! image_version_compatible "$1" \
    || image_too_old "$1" \
    || { [ "$SELF_HEAL" = 1 ] && [ "$IMAGE_STATE_PRESENT" = 1 ] \
         && ! image_state_matches "$1"; }
}

cd "$REPO_DIR/sandbox-image"
# Stamp the zero-drift version (commit SHA) so /etc/sandbox-image-version +
# /drift report the build that produced the image, matching the EC2 job.
# aidream builds FROM matrx-sandbox:core, so it inherits this version file.
CORE_BUILD_VERSION=core
CORE_REBUILT=0
if need_img matrx-sandbox:core; then
  CORE_CANDIDATE="matrx-sandbox:core-$NEW_SHA"
  build_candidate matrx-sandbox:core "$CORE_CANDIDATE" \
    docker build --build-arg MATRX_IMAGE_VERSION="$NEW_SHA" -t "$CORE_CANDIDATE" .
  CORE_BUILD_VERSION="core-$NEW_SHA"
  CORE_REBUILT=1
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
if [ "$CORE_REBUILT" = 1 ] || need_img matrx-sandbox:aidream || aidream_stale; then
  AIDREAM_SOURCE_SHA=$(resolve_aidream_source_sha || true)
  [[ "$AIDREAM_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || fail "cannot resolve immutable aidream source SHA"
  AIDREAM_CANDIDATE="matrx-sandbox:aidream-$NEW_SHA-${AIDREAM_SOURCE_SHA:0:12}"
  stamp_aidream_rebuild
  build_candidate matrx-sandbox:aidream "$AIDREAM_CANDIDATE" \
    env MATRX_IMAGE_VERSION="$NEW_SHA" MATRX_CORE_VERSION="$CORE_BUILD_VERSION" \
      bash build-aidream.sh --tag "$AIDREAM_CANDIDATE" \
        --source-sha "$AIDREAM_SOURCE_SHA"
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
validate_release_authority
if [ "$ORCH_CHANGED" = 1 ]; then
  run_db_migrations "$ORCH_CANDIDATE"
  validate_release_authority
fi

ORCH_API_KEY=$(grep '^MATRX_API_KEY=' "$ORCH_COMPOSE_DIR/.env" | head -1 | cut -d= -f2-)
[ -n "$ORCH_API_KEY" ] || fail "MATRX_API_KEY is not resolved for release verification"
PREVIOUS_ORCH_CONTAINER_ID=$(cd "$ORCH_COMPOSE_DIR" && docker compose ps -q orchestrator)
[ -n "$PREVIOUS_ORCH_CONTAINER_ID" ] \
  || fail "cannot identify the live orchestrator before release"
PREVIOUS_ORCH_IMAGE_ID=$(docker inspect -f '{{.Image}}' "$PREVIOUS_ORCH_CONTAINER_ID" 2>/dev/null)
[[ "$PREVIOUS_ORCH_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || fail "live orchestrator image identity is invalid"
PREVIOUS_ORCH_SOURCE=$(docker image inspect "$PREVIOUS_ORCH_IMAGE_ID" \
  --format '{{index .Config.Labels "com.aimatrx.source.sha"}}' 2>/dev/null || true)
[[ "$PREVIOUS_ORCH_SOURCE" =~ ^[0-9a-f]{40}$ ]] \
  || fail "live orchestrator source identity is invalid"

orchestrator_contract_matches() {
  local expected_sha="$1" require_barrier="${2:-1}" payload
  payload=$(curl -fsS --max-time 5 -H "X-API-Key: $ORCH_API_KEY" \
    "${ORCH_HEALTH_URL%/health}/api-surface" 2>/dev/null) || return 1
  RELEASE_PAYLOAD="$payload" EXPECTED_SHA="$expected_sha" \
    REQUIRE_BARRIER="$require_barrier" python3 - <<'PY'
import json, os
d = json.loads(os.environ["RELEASE_PAYLOAD"])
paths = {r["path"] for r in d.get("routes", [])}
required = {"/sandboxes/{sandbox_id}/fs/{path:path}", "/sandboxes/{sandbox_id}/fs/watch"}
assert d.get("source_sha") == os.environ["EXPECTED_SHA"]
assert d.get("contracts", {}).get("filesystem") == 2
assert required <= paths
if os.environ["REQUIRE_BARRIER"] == "1":
    assert d.get("contracts", {}).get("deployment_migration_barrier") == 1
PY
}

wait_for_orchestrator_contract() {
  local expected_sha="$1" require_barrier="${2:-1}" deadline remaining request_timeout
  deadline=$((SECONDS + ORCH_STARTUP_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if orchestrator_contract_matches "$expected_sha" "$require_barrier"; then
      return 0
    fi
    remaining=$((deadline - SECONDS)); (( remaining > 0 )) || break
    request_timeout=$((remaining < 2 ? remaining : 2))
    sleep "$request_timeout"
  done
  return 1
}

audit_migration_release_state() {
  local image="$1" socket_gid
  socket_gid=$(stat -c '%g' /var/run/docker.sock) \
    || return 1
  docker run --rm --network none --group-add "$socket_gid" \
    --mount "type=bind,src=$HOSTED_MIGRATION_STATE_DIR,dst=/var/lib/matrx-sandbox/hosted-migrations" \
    --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
    "$image" python -m orchestrator.release_barrier audit
}

required_migration_lock_keys() {
  local image="$1"
  docker run --rm --network none \
    --mount "type=bind,src=$HOSTED_MIGRATION_STATE_DIR,dst=/var/lib/matrx-sandbox/hosted-migrations" \
    "$image" python -m orchestrator.release_barrier lock-keys
}

MIGRATION_LOCK_HOLDER_PID=""
MIGRATION_LOCK_HOLDER_INPUT_FD=""
MIGRATION_LOCK_HOLDER_OUTPUT_FD=""

release_migration_lock_holder() {
  local status=0
  if [ -n "$MIGRATION_LOCK_HOLDER_INPUT_FD" ]; then
    eval "exec ${MIGRATION_LOCK_HOLDER_INPUT_FD}>&-" || status=1
    MIGRATION_LOCK_HOLDER_INPUT_FD=""
  fi
  if [ -n "$MIGRATION_LOCK_HOLDER_OUTPUT_FD" ]; then
    eval "exec ${MIGRATION_LOCK_HOLDER_OUTPUT_FD}<&-" || status=1
    MIGRATION_LOCK_HOLDER_OUTPUT_FD=""
  fi
  if [ -n "$MIGRATION_LOCK_HOLDER_PID" ]; then
    wait "$MIGRATION_LOCK_HOLDER_PID" || status=1
    MIGRATION_LOCK_HOLDER_PID=""
  fi
  return "$status"
}

start_migration_lock_holder() {
  local scope="$1" wait_seconds="$2" receipt status
  shift 2
  local -a options=(--wait-seconds "$wait_seconds")
  [ "$scope" = all ] && options+=(--all-existing)
  [ -z "$MIGRATION_LOCK_HOLDER_PID" ] || return 76
  coproc {
    python3 "$REPO_DIR/orchestrator/orchestrator/release_lock_holder.py" \
      "${options[@]}" "$HOSTED_MIGRATION_STATE_DIR" "$@"
  }
  MIGRATION_LOCK_HOLDER_PID=$COPROC_PID
  MIGRATION_LOCK_HOLDER_OUTPUT_FD=${COPROC[0]}
  MIGRATION_LOCK_HOLDER_INPUT_FD=${COPROC[1]}
  if ! IFS= read -r receipt <&"$MIGRATION_LOCK_HOLDER_OUTPUT_FD"; then
    if wait "$MIGRATION_LOCK_HOLDER_PID"; then status=0; else status=$?; fi
    MIGRATION_LOCK_HOLDER_PID=""
    eval "exec ${MIGRATION_LOCK_HOLDER_OUTPUT_FD}<&-" || true
    eval "exec ${MIGRATION_LOCK_HOLDER_INPUT_FD}>&-" || true
    MIGRATION_LOCK_HOLDER_OUTPUT_FD=""
    MIGRATION_LOCK_HOLDER_INPUT_FD=""
    return "$status"
  fi
  printf '%s' "$receipt" | python3 -c \
    'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="ready" and isinstance(d.get("uid"),int) and isinstance(d.get("gid"),int)' \
    || { release_migration_lock_holder || true; return 76; }
  eval "exec ${MIGRATION_LOCK_HOLDER_OUTPUT_FD}<&-"
  MIGRATION_LOCK_HOLDER_OUTPUT_FD=""
}

# The exact set of lock inodes in the journal directory. The bootstrap seize
# happens while the old orchestrator still RUNS, so this census is taken at the
# moment the lease is held and compared again once the old control is frozen:
# a lockless source that admitted a new operation in that window shows up as a
# new *.lock name, and the promotion defers instead of cutting it off.
lock_census() {
  ( cd "$HOSTED_MIGRATION_STATE_DIR" 2>/dev/null \
      && ls -1 2>/dev/null | grep '\.lock$' | LC_ALL=C sort | tr '\n' ' ' ) || true
}

acquire_frozen_old_locks() {
  local audit_image="$1" wait_seconds="${2:-0}" key derived status
  local -a keys=(deployment)
  derived=$(required_migration_lock_keys "$audit_image") || return 2
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    if ! [[ "$key" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$ ]]; then
      return 2
    fi
    keys+=("$key")
  done <<< "$derived"
  if start_migration_lock_holder all "$wait_seconds" "${keys[@]}"; then return 0; else status=$?; fi
  [ "$status" = 75 ] && return 1
  return 2
}

resume_bootstrap_old() {
  [ "${BOOTSTRAP_OLD_PAUSED:-0}" = 1 ] || return 0
  release_migration_lock_holder
  docker unpause "$BOOTSTRAP_OLD_ID" >/dev/null \
    || return 1
  BOOTSTRAP_OLD_PAUSED=0
  orchestrator_contract_matches "$PREVIOUS_ORCH_SOURCE" 0
}

rollback_release() {
  log "rolling back all live tags from the failed release"
  local index live
  if [ "${BOOTSTRAP_OLD_PAUSED:-0}" = 1 ]; then
    resume_bootstrap_old \
      || log "ERROR: failed to resume exact pre-promotion orchestrator $BOOTSTRAP_OLD_ID"
    return
  fi
  for live in "${PROMOTED_TAGS[@]}"; do
    if docker image inspect "${live}-rollback" >/dev/null 2>&1; then
      docker tag "${live}-rollback" "$live"
    else
      docker image rm "$live" >/dev/null 2>&1 || true
    fi
  done
  if [ "$ORCH_CHANGED" = 1 ]; then
    if [[ "${PREVIOUS_ORCH_IMAGE_ID:-}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      docker tag "$PREVIOUS_ORCH_IMAGE_ID" "$ORCH_IMAGE"
    elif docker image inspect matrx-orchestrator:rollback >/dev/null 2>&1; then
      docker tag matrx-orchestrator:rollback "$ORCH_IMAGE"
    else
      docker image rm "$ORCH_IMAGE" >/dev/null 2>&1 || true
    fi
  fi
  if [ -n "${LOCAL_CANDIDATE:-}" ] && [ -f "$REPO_DIR/sandbox-local/docker-compose.yml" ]; then
    ( cd "$REPO_DIR/sandbox-local" && docker compose up -d ) || true
  fi
  # Final mutation on rollback: start the exact previous source.  Nothing
  # below may signal, retag, recreate, reload configuration, or mutate images.
  if [ "${ORCH_STOPPED:-0}" = 1 ]; then
    if ! ( cd "$ORCH_COMPOSE_DIR" && docker compose up -d --force-recreate ); then
      log "ERROR: rollback could not start the previous orchestrator"
      release_migration_lock_holder || true
      return
    fi
    if ! wait_for_orchestrator_contract "$PREVIOUS_ORCH_SOURCE" 0; then
      log "ERROR: rollback source/health verification failed for $PREVIOUS_ORCH_SOURCE"
    fi
  fi
  release_migration_lock_holder || log "ERROR: rollback could not release migration lock holder"
}
fail_release() { PROMOTION_ACTIVE=0; rollback_release; fail "$*"; }

bootstrap_barrier_seize() {
  local lock_status=0 seized_census kill_status old_stopped
  BOOTSTRAP_OLD_ID="$PREVIOUS_ORCH_CONTAINER_ID"
  [ -n "$BOOTSTRAP_OLD_ID" ] \
    || fail_release "cannot identify the live orchestrator for barrier bootstrap"
  # 1. Seize every old-source operation lock while the old control still runs.
  #    Nothing about the live service has been touched yet, so any failure here
  #    defers a promotion instead of costing the edge a paused orchestrator.
  acquire_frozen_old_locks "$AUDIT_IMAGE" 0 || lock_status=$?
  if [ "$lock_status" = 1 ]; then
    fail_release "old-source migration/recovery lock contention deferred hosted promotion (orchestrator untouched)"
  elif [ "$lock_status" != 0 ]; then
    fail_release "old-source migration lock inventory is invalid (orchestrator untouched)"
  fi
  seized_census="$(lock_census)"
  # 2. Only now freeze the exact old control.
  docker pause "$BOOTSTRAP_OLD_ID" >/dev/null \
    || fail_release "cannot pause the exact old orchestrator for barrier bootstrap"
  BOOTSTRAP_OLD_PAUSED=1
  [ "$(docker inspect -f '{{.State.Status}}' "$BOOTSTRAP_OLD_ID" 2>/dev/null)" = paused ] \
    || fail_release "exact old orchestrator did not reach paused state"
  # 3. Prove the frozen source admitted nothing between the seize and the pause.
  [ "$(lock_census)" = "$seized_census" ] \
    || fail_release "the old orchestrator admitted a new operation during the bootstrap seize window"
  audit_migration_release_state "$AUDIT_IMAGE" \
    || fail_release "hosted migration journal/artifact census refused bootstrap"
  kill_status=0
  docker kill --signal KILL "$BOOTSTRAP_OLD_ID" >/dev/null || kill_status=$?
  old_stopped=0
  for _ in $(seq 1 50); do
    if [ "$(docker inspect -f '{{.State.Running}}' "$BOOTSTRAP_OLD_ID" 2>/dev/null)" = false ]; then
      old_stopped=1; break
    fi
    sleep 0.1
  done
  if [ "$old_stopped" = 1 ]; then
    BOOTSTRAP_OLD_PAUSED=0
    ORCH_STOPPED=1
  fi
  [ "$kill_status" = 0 ] && [ "$old_stopped" = 1 ] \
    || fail_release "fatal bootstrap stop did not terminate the exact old orchestrator"
}

PROMOTED_TAGS=()
PROMOTION_ACTIVE=1
ORCH_STOPPED=0
trap 'status=$?; trap - EXIT INT TERM; if [ "${PROMOTION_ACTIVE:-0}" = 1 ]; then PROMOTION_ACTIVE=0; rollback_release; fi; clear_all_build_markers; exit $status' EXIT INT TERM
if [ "$ORCH_CHANGED" = 1 ] || [ "${#LIVE_TAGS[@]}" -gt 0 ]; then
  # Migrations/recovery hold a shared lock from durable admission through
  # terminal cleanup. Promotion takes the exclusive peer before stopping the
  # orchestrator, closing the check/stop race that previously SIGKILLed a
  # healthy long-running backup. A failed nonblocking acquisition leaves the
  # live release untouched; the authoritative poller retries later.
  AUDIT_IMAGE="${ORCH_CANDIDATE:-$ORCH_IMAGE}"
  if orchestrator_contract_matches "$PREVIOUS_ORCH_SOURCE" 1; then
    lock_status=0
    start_migration_lock_holder named "$DEPLOY_LOCK_WAIT_SECONDS" deployment \
      || lock_status=$?
    if [ "$lock_status" = 75 ]; then
      fail_release "active sandbox migration deferred hosted promotion after ${DEPLOY_LOCK_WAIT_SECONDS}s of waiting (knob: DEPLOY_LOCK_WAIT_SECONDS)"
    elif [ "$lock_status" != 0 ]; then
      fail_release "hosted deployment lock inventory is invalid"
    fi
    audit_migration_release_state "$AUDIT_IMAGE" \
      || fail_release "hosted migration journal/artifact census refused promotion"
    # A barrier-capable source cannot have an admitted operation after EX lock
    # acquisition, so its normal shutdown has no recovery child to interrupt.
    ORCH_STOPPED=1
    ( cd "$ORCH_COMPOSE_DIR" && docker compose stop ) \
      || fail_release "could not enter the release promotion window"
  else
    # Bootstrap from lockless 474 — SEIZE FIRST, PAUSE ONLY AFTER.
    # The old order paused the exact old control before it knew whether the
    # locks were free, so a contended seize (or an invalid inventory, or a slow
    # `lock-keys` container start) froze the LIVE orchestrator for the whole
    # attempt and then rolled back — the edge paid for a deploy that never
    # happened. The seize is harmless while the old control runs: on failure we
    # defer with the orchestrator untouched. What the pause used to buy — no new
    # operation admitted between the census and the freeze — is bought instead
    # by re-censusing the lock set once the control IS frozen (lock_census).
    bootstrap_barrier_seize
  fi
fi
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
  clear_build_marker "${live##*:}"
done

if [ "$ORCH_CHANGED" = 1 ]; then
  if docker image inspect "$PREVIOUS_ORCH_IMAGE_ID" >/dev/null 2>&1; then
    docker tag "$PREVIOUS_ORCH_IMAGE_ID" matrx-orchestrator:rollback
  else
    docker image rm matrx-orchestrator:rollback >/dev/null 2>&1 || true
  fi
  docker tag "$ORCH_CANDIDATE" "$ORCH_IMAGE" \
    || fail_release "could not promote $ORCH_CANDIDATE"
  clear_build_marker orchestrator
fi

if [ "$ORCH_STOPPED" = 1 ]; then
  ( cd "$ORCH_COMPOSE_DIR" && docker compose up -d --force-recreate ) \
    || fail_release "orchestrator recreate failed"
fi

if [ -n "${LOCAL_CANDIDATE:-}" ] && [ -f "$REPO_DIR/sandbox-local/docker-compose.yml" ]; then
  log "recreating local starter pool (sandbox-1..5) on the promoted image"
  ( cd "$REPO_DIR/sandbox-local" && docker compose up -d ) \
    || fail_release "starter pool recreate failed"
fi

# A measured 229-row fleet startup took 157s. Budget elapsed time, not attempts
# (each curl can consume five seconds); keep exact source/API verification.
log "waiting for orchestrator release contract (up to ${ORCH_STARTUP_TIMEOUT_SECONDS}s)…"
verified=0
ORCH_WAIT_STARTED=$SECONDS
ORCH_WAIT_DEADLINE=$((SECONDS + ORCH_STARTUP_TIMEOUT_SECONDS))
while (( SECONDS < ORCH_WAIT_DEADLINE )); do
  remaining=$((ORCH_WAIT_DEADLINE - SECONDS))
  (( remaining > 0 )) || break
  request_timeout=$((remaining < 5 ? remaining : 5))
  if payload=$(curl -fsS --max-time "$request_timeout" -H "X-API-Key: $ORCH_API_KEY" \
      "${ORCH_HEALTH_URL%/health}/api-surface" 2>/dev/null) \
      && RELEASE_PAYLOAD="$payload" EXPECTED_SHA="$NEW_SHA" python3 - <<'PY'
import json, os
d = json.loads(os.environ["RELEASE_PAYLOAD"])
paths = {r["path"] for r in d.get("routes", [])}
required = {"/sandboxes/{sandbox_id}/fs/{path:path}", "/sandboxes/{sandbox_id}/fs/watch"}
assert d.get("source_sha") == os.environ["EXPECTED_SHA"]
assert d.get("contracts", {}).get("filesystem") == 2
assert d.get("contracts", {}).get("deployment_migration_barrier") == 1
assert required <= paths
PY
  then
    if (( SECONDS <= ORCH_WAIT_DEADLINE )); then verified=1; fi
    break
  fi
  remaining=$((ORCH_WAIT_DEADLINE - SECONDS))
  (( remaining > 0 )) || break
  sleep "$((remaining < 2 ? remaining : 2))"
done
ORCH_WAIT_ELAPSED=$((SECONDS - ORCH_WAIT_STARTED))
[ "$verified" = 1 ] || fail_release "exact source/API/filesystem contract verification failed after ${ORCH_WAIT_ELAPSED}s (startup budget ${ORCH_STARTUP_TIMEOUT_SECONDS}s)"
log "release contract verified at $NEW_SHA in ${ORCH_WAIT_ELAPSED}s (budget ${ORCH_STARTUP_TIMEOUT_SECONDS}s) ✓"
release_migration_lock_holder

# Refresh the out-of-checkout poller and its timeout policy only after this
# release is healthy. Installing only the runner previously left the live
# systemd unit pinned to a 45-minute timeout that killed valid cold builds.
install -m 0755 "$REPO_DIR/scripts/systemd/pull-deploy-runner.sh" \
  /usr/local/bin/matrx-hosted-deploy-runner \
  || fail_release "could not install approved-ref poller"
install -m 0644 "$REPO_DIR/scripts/systemd/matrx-hosted-deploy.service" \
  /etc/systemd/system/matrx-hosted-deploy.service \
  || fail_release "could not install hosted deploy service"
install -m 0644 "$REPO_DIR/scripts/systemd/matrx-hosted-deploy.timer" \
  /etc/systemd/system/matrx-hosted-deploy.timer \
  || fail_release "could not install hosted deploy timer"
systemctl daemon-reload \
  || fail_release "could not reload hosted deploy systemd units"
systemctl enable --now matrx-hosted-deploy.timer \
  || fail_release "could not enable hosted deploy timer"

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
IMAGE_STATE_TMP="${IMAGE_STATE_FILE}.tmp.$$"
: > "$IMAGE_STATE_TMP" || fail_release "could not stage hosted image state"
for live in \
  "$ORCH_IMAGE" \
  matrx-sandbox:core \
  matrx-sandbox:slim \
  matrx-sandbox:aidream \
  matrx-sandbox:local; do
  image_id=$(docker image inspect "$live" --format '{{.Id}}' 2>/dev/null) \
    || fail_release "required live image vanished before state commit: $live"
  printf '%s\t%s\n' "$live" "$image_id" >> "$IMAGE_STATE_TMP" \
    || fail_release "could not stage hosted image state for $live"
done
mv -f "$IMAGE_STATE_TMP" "$IMAGE_STATE_FILE" \
  || fail_release "could not atomically record hosted image state"
STATE_TMP="${STATE_FILE}.tmp.$$"
printf '%s\n' "$NEW_SHA" > "$STATE_TMP" \
  && mv -f "$STATE_TMP" "$STATE_FILE" \
  || fail_release "could not atomically record deployed SHA"
PROMOTION_ACTIVE=0
ORCH_STOPPED=0
clear_all_build_markers
clear_failure
trap - EXIT INT TERM
log "hosted-tier deploy complete at $NEW_SHA"
