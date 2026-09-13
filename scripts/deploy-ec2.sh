#!/usr/bin/env bash
# Atomically promote one tested matrx-sandbox commit on the EC2 orchestrator host.
# Invoked by GitHub Actions through SSM after the exact commit has been cloned.
set -euo pipefail

TARGET_SHA="${1:-}"
ECR_REPO="${2:-}"
RELEASE_ROOT="${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LIVE_DIR=/home/ec2-user/orchestrator
CANDIDATE_DIR="/home/ec2-user/orchestrator-candidate-$TARGET_SHA"
ROLLBACK_DIR=/home/ec2-user/orchestrator-rollback
FAILED_DIR="/home/ec2-user/orchestrator-failed-$TARGET_SHA"
UNIT=matrx-orchestrator
UV_VERSION=0.10.8
DROPIN_DIR=/etc/systemd/system/matrx-orchestrator.service.d
RELEASE_DROPIN="$DROPIN_DIR/release.conf"
DROPIN_BACKUP="/tmp/matrx-orchestrator-release-conf-$TARGET_SHA"
# Exact production revisions served by the copy-in-place workflow before
# atomic, revision-stamped deployments. Bootstrap accepts no other tree.
LEGACY_EC2_SOURCE_SHAS="30ed118b431b72e8f73f1b199fd9398d78361ed5 f229d4b9347a66b3e8e8d8235f122d31dc336436"

log() { echo "[deploy-ec2] $*"; }
fail() { echo "[deploy-ec2] ERROR: $*" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/release-guard.sh
source "$SCRIPT_DIR/lib/release-guard.sh" \
  || fail "cannot load release ancestry guard"
release_guard_validate_sha "$TARGET_SHA" "target"
[ -n "$ECR_REPO" ] || fail "ECR repository is required"
[ "$(git -C "$RELEASE_ROOT" rev-parse HEAD)" = "$TARGET_SHA" ] \
  || fail "release checkout does not match $TARGET_SHA"
exec 9>/var/lock/matrx-sandbox-deploy.lock
flock -n 9 || fail "another EC2 release is already running"

# Migrations are forward-only. Re-check the immutable CI approval and deployed
# code at every irreversible boundary. A later, unapproved main commit must not
# strand an already approved rollout, while stale/downgrade releases stay shut.
validate_release_authority() {
  release_guard_fetch_approved_release "$RELEASE_ROOT" "$TARGET_SHA"
  if [ -d "$LIVE_DIR" ]; then
    if [ ! -e "$LIVE_DIR/.source-sha" ] && [ ! -L "$LIVE_DIR/.source-sha" ]; then
      log "verifying one-time legacy live-source bootstrap"
      release_guard_bootstrap_legacy_source \
        "$RELEASE_ROOT" "$LIVE_DIR" "$LEGACY_EC2_SOURCE_SHAS" orchestrator
    fi
    [ -f "$LIVE_DIR/.source-sha" ] && [ ! -L "$LIVE_DIR/.source-sha" ] \
      && [ -r "$LIVE_DIR/.source-sha" ] \
      || fail "live orchestrator source revision is unreadable"
    DEPLOYED_SHA=$(tr -d '[:space:]' < "$LIVE_DIR/.source-sha")
    release_guard_assert_descendant \
      "$RELEASE_ROOT" "$DEPLOYED_SHA" "$TARGET_SHA" "deployed EC2 revision"
  fi
}
validate_release_authority
# Keep the pre-promotion source identity for rollback proof.  The barrier is
# entered before any source/drop-in mutation, so bootstrap deferral leaves it
# untouched; rollback additionally verifies it after restarting the old unit.
ORIGINAL_SOURCE_SHA="${DEPLOYED_SHA:-}"

resolve_setting() {
  local name="$1" value
  value=$(systemctl show "$UNIT" -p Environment --value 2>/dev/null \
    | tr ' ' '\n' | sed -n "s/^${name}=//p" | head -1)
  if [ -z "$value" ] && [ -r "$LIVE_DIR/.env" ]; then
    value=$(sed -n "s/^${name}=//p" "$LIVE_DIR/.env" | head -1)
  fi
  printf '%s' "$value"
}

DB_URL=$(resolve_setting MATRX_DATABASE_URL)
API_KEY=$(resolve_setting MATRX_API_KEY)
STORE=$(resolve_setting MATRX_SANDBOX_STORE)
HOST_TIER=$(resolve_setting MATRX_HOST_TIER)
AIDREAM_URL=$(resolve_setting MATRX_AIDREAM_URL)
EXPECTED_AIDREAM_URL="http://aidream.internal.matrxserver.com"
[ -n "$DB_URL" ] || fail "MATRX_DATABASE_URL is unresolved; migrations may not be skipped"
[ -n "$API_KEY" ] || fail "MATRX_API_KEY is unresolved; production metadata must stay authenticated"
# Pre-flight for the store guard in orchestrator/config.py: the new container
# would refuse to boot on anything but 'postgres' here. Catch it BEFORE the
# swap instead of after.
[ "$STORE" = "postgres" ] \
  || fail "MATRX_SANDBOX_STORE is '${STORE:-unset}'; a deployed orchestrator must set it to 'postgres' (in-memory loses every sandbox row on restart)"
# Token issuance, lifecycle reconciliation, and persistence layout all require
# an exact tier. Catch a missing/wrong value before the container swap instead
# of letting /access-tokens fail as an opaque HTTP 500 later.
[ "$HOST_TIER" = "ec2" ] \
  || fail "MATRX_HOST_TIER is '${HOST_TIER:-unset}'; the EC2 orchestrator must set it to 'ec2'"
# EC2-origin sandbox traffic must reach AI Dream through its private ECS endpoint.
# Routing through the public endpoint defeats the intended private network path
# and makes an internal dependency depend on public DNS.
[ "$AIDREAM_URL" = "$EXPECTED_AIDREAM_URL" ] \
  || fail "MATRX_AIDREAM_URL is '${AIDREAM_URL:-unset}'; EC2 must use $EXPECTED_AIDREAM_URL, never the public app_server"

# Immutable per-SHA tags are deleted at the end of a SUCCESSFUL release, so a
# failed one leaks ~4 GB of them. A few bad releases fill the 50 GB root volume
# and every later deploy dies inside `docker pull` with the useless "failed to
# register layer: no space left on device" (2026-08-09 and 2026-08-11 both died
# this way, leaving EC2 three releases behind). Reclaim leaked candidates
# first, then refuse to start — loudly, naming the disk — rather than dying
# halfway through a pull.
cleanup_candidate_images() {
  docker image rm \
    "$ECR_REPO:$TARGET_SHA" \
    "$ECR_REPO:slim-$TARGET_SHA" \
    "$ECR_REPO:development-$TARGET_SHA" \
    "$ECR_REPO-orchestrator:$TARGET_SHA" >/dev/null 2>&1 || true
}

log "reclaiming disk from previous releases"
# `|| true` matters: under `set -o pipefail` a grep that matches nothing — the
# normal, healthy case where no candidates leaked — fails the pipeline and
# would abort the release before it starts. Nothing to reclaim is success.
LEAKED=$(docker images --format '{{.Repository}}:{{.Tag}}' \
  | grep -E "^${ECR_REPO}(-orchestrator)?:(slim-|development-)?[0-9a-f]{40}$" \
  | grep -v ":\(slim-\|development-\)\?$TARGET_SHA\$" || true)
for leaked in $LEAKED; do
  log "removing leaked release candidate $leaked"
  docker image rm "$leaked" >/dev/null 2>&1 || true
done
docker image prune -f >/dev/null 2>&1 || true
docker builder prune -f >/dev/null 2>&1 || true

REQUIRED_FREE_KB=$((10 * 1024 * 1024))   # 10 GiB — the three pulls land ~5 GiB
# Ask Docker where its data actually lives, and never let the guard itself be
# the thing that fails a release: an unreadable path falls back to /.
DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
[ -d "$DOCKER_ROOT" ] || DOCKER_ROOT=/
FREE_KB=$(df -Pk "$DOCKER_ROOT" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "${FREE_KB:-}" ] && [ "$FREE_KB" -lt "$REQUIRED_FREE_KB" ]; then
  df -h "$DOCKER_ROOT" >&2
  docker system df >&2 || true
  fail "only $((FREE_KB / 1024)) MiB free on the Docker filesystem, need $((REQUIRED_FREE_KB / 1024)) MiB — release $TARGET_SHA not attempted; free space on the EC2 box and re-run the Deploy workflow"
fi
if [ -n "${FREE_KB:-}" ]; then
  log "disk ok: $((FREE_KB / 1024)) MiB free on $DOCKER_ROOT"
else
  log "WARNING: could not read free space for $DOCKER_ROOT — proceeding unguarded"
fi

log "pulling immutable image candidates"
trap 'cleanup_candidate_images' ERR INT TERM
docker pull "$ECR_REPO:$TARGET_SHA"
docker pull "$ECR_REPO:slim-$TARGET_SHA"
docker pull "$ECR_REPO:development-$TARGET_SHA"
docker pull "$ECR_REPO-orchestrator:$TARGET_SHA"
for image in \
  "$ECR_REPO:$TARGET_SHA" \
  "$ECR_REPO:slim-$TARGET_SHA" \
  "$ECR_REPO:development-$TARGET_SHA"; do
  baked=$(docker image inspect "$image" \
    --format '{{index .Config.Labels "com.aimatrx.sandbox.version"}}')
  [ "$baked" = "$TARGET_SHA" ] || fail "$image embeds unexpected version $baked"
done
baked=$(docker image inspect "$ECR_REPO-orchestrator:$TARGET_SHA" \
  --format '{{index .Config.Labels "com.aimatrx.source.sha"}}')
[ "$baked" = "$TARGET_SHA" ] || fail "orchestrator image embeds unexpected source $baked"

log "staging locked orchestrator environment"
rm -rf "$CANDIDATE_DIR" "$FAILED_DIR"
install -d -o ec2-user -g ec2-user "$CANDIDATE_DIR"
cp -a "$RELEASE_ROOT/orchestrator/." "$CANDIDATE_DIR/"
[ ! -r "$LIVE_DIR/.env" ] || cp -a "$LIVE_DIR/.env" "$CANDIDATE_DIR/.env"
printf '%s\n' "$TARGET_SHA" > "$CANDIDATE_DIR/.source-sha"
chown -R ec2-user:ec2-user "$CANDIDATE_DIR"
sudo -u ec2-user /usr/bin/python3.11 -m pip install --user --quiet "uv==$UV_VERSION"
sudo -u ec2-user env PATH="/home/ec2-user/.local/bin:$PATH" \
  uv sync --directory "$CANDIDATE_DIR" --locked --no-dev --python /usr/bin/python3.11
# The venv is built at $CANDIDATE_DIR and then MOVED to $LIVE_DIR, so anything
# that hardcodes the build path breaks on arrival. Prove the one entry point
# the unit actually uses survives that move; console scripts do not (their
# shebang is the absolute candidate path), which is why the drop-in below
# execs `python -m uvicorn` instead of `.venv/bin/uvicorn`.
sudo -u ec2-user "$CANDIDATE_DIR/.venv/bin/python" -m uvicorn --version >/dev/null \
  || fail "candidate venv cannot run 'python -m uvicorn' — refusing to promote a release that cannot boot"

# Durable recovery state is a host directory explicitly bound into the service
# namespace. Never substitute an ephemeral service cwd or chmod existing state.
JOURNAL_DIR=/var/lib/matrx-sandbox/hosted-migrations
[ ! -L /var/lib/matrx-sandbox ] && [ ! -L "$JOURNAL_DIR" ] \
  || fail "migration journal path is a symlink"
install -d -o root -g root -m 0755 /var/lib/matrx-sandbox
if [ ! -e "$JOURNAL_DIR" ]; then
  install -d -o ec2-user -g ec2-user -m 0700 "$JOURNAL_DIR"
fi
sudo -u ec2-user /usr/bin/python3.11 - "$JOURNAL_DIR" <<'PY'
import os, stat, sys, tempfile
p=sys.argv[1]; s=os.lstat(p)
assert stat.S_ISDIR(s.st_mode) and s.st_uid == os.geteuid() and not s.st_mode & 0o077
fd,n=tempfile.mkstemp(dir=p)
try: os.write(fd,b'preflight'); os.fsync(fd)
finally: os.close(fd); os.unlink(n)
PY
command -v getfacl >/dev/null || yum install -y acl
install -d -o root -g root -m 0755 /usr/local/libexec
HELPER_DIGEST=$(sha256sum "$CANDIDATE_DIR/orchestrator/ec2_home_copy.py" | cut -d' ' -f1)
HELPER="/usr/local/libexec/matrx-ec2-home-copy-$HELPER_DIGEST.py"
[ ! -L "$HELPER" ] || fail "home-copy helper is a symlink"
if [ ! -e "$HELPER" ]; then
  install -o root -g root -m 0755 "$CANDIDATE_DIR/orchestrator/ec2_home_copy.py" "$HELPER"
fi
cmp "$CANDIDATE_DIR/orchestrator/ec2_home_copy.py" "$HELPER" \
  || fail "home-copy helper differs from approved candidate"
sudo -u ec2-user sudo -n env -i PATH=/usr/bin:/bin \
  /usr/bin/python3.11 -I "$HELPER" --preflight \
  || fail "home-copy helper is unavailable to actual service user"
docker image inspect "$ECR_REPO-orchestrator:$TARGET_SHA" --format '{{.Id}}' \
  > "$CANDIDATE_DIR/.migration-helper-image"
chown ec2-user:ec2-user "$CANDIDATE_DIR/.migration-helper-image"
systemd-run --quiet --wait --pipe --collect \
  --unit="matrx-home-preflight-$TARGET_SHA" \
  -p User=ec2-user -p "BindPaths=$JOURNAL_DIR" -p "WorkingDirectory=$CANDIDATE_DIR" \
  "$CANDIDATE_DIR/.venv/bin/python" -c \
  'import asyncio; from orchestrator.hosted_migration import HostedMigrationJournal; from orchestrator.ec2_home_copy_client import preflight_helper; HostedMigrationJournal().ensure_ready(); asyncio.run(preflight_helper())' \
  || fail "candidate service-user journal/helper preflight failed"

log "applying required migrations before promotion"
validate_release_authority
sudo -u ec2-user env MATRX_DATABASE_URL="$DB_URL" \
  "$CANDIDATE_DIR/.venv/bin/python" -m orchestrator.migrate_runner
validate_release_authority

DEPLOYMENT_LOCK="$JOURNAL_DIR/deployment.lock"
RUNTIME_DROPIN_DIR="/run/systemd/system/${UNIT}.service.d"
RUNTIME_DROPIN="$RUNTIME_DROPIN_DIR/matrx-deployment-bootstrap.conf"
CGROUP_ROOT="${CGROUP_ROOT:-/sys/fs/cgroup}"
BOOTSTRAP_ACTIVE=0
BOOTSTRAP_STOPPED=0
BOOTSTRAP_KILLED=0
BOOTSTRAP_CGROUP=""
BOOTSTRAP_PID=""
BOOTSTRAP_PROCS=""
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
  local scope="$1" receipt status
  shift
  local -a options=()
  [ "$scope" = all ] && options+=(--all-existing)
  [ -z "$MIGRATION_LOCK_HOLDER_PID" ] || return 76
  coproc {
    sudo -u ec2-user "$CANDIDATE_DIR/.venv/bin/python" \
      "$CANDIDATE_DIR/orchestrator/release_lock_holder.py" \
      "${options[@]}" "$JOURNAL_DIR" "$@"
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
  printf '%s' "$receipt" | "$CANDIDATE_DIR/.venv/bin/python" -c \
    'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="ready" and isinstance(d.get("uid"),int) and isinstance(d.get("gid"),int)' \
    || { release_migration_lock_holder || true; return 76; }
  eval "exec ${MIGRATION_LOCK_HOLDER_OUTPUT_FD}<&-"
  MIGRATION_LOCK_HOLDER_OUTPUT_FD=""
}

release_barrier_audit() {
  # The candidate owns record parsing/census. This is deliberately read-only:
  # deploy must not decide from filenames or terminal phases alone.
  systemd-run --quiet --wait --pipe --collect \
    --unit="matrx-release-barrier-audit-${TARGET_SHA:0:12}-$$" \
    -p User=ec2-user -p "BindPaths=$JOURNAL_DIR" -p "WorkingDirectory=$CANDIDATE_DIR" \
    "$CANDIDATE_DIR/.venv/bin/python" -m orchestrator.release_barrier audit
}

release_barrier_lock_keys() {
  systemd-run --quiet --wait --pipe --collect \
    --unit="matrx-release-barrier-locks-${TARGET_SHA:0:12}-$$" \
    -p User=ec2-user -p "BindPaths=$JOURNAL_DIR" -p "WorkingDirectory=$CANDIDATE_DIR" \
    "$CANDIDATE_DIR/.venv/bin/python" -m orchestrator.release_barrier lock-keys
}

bootstrap_fail() {
  # `fail` exits directly, which does not invoke Bash's ERR trap. Once this
  # bootstrap has changed the live unit, restore its exact pre-stop runtime
  # before reporting a deferred/integrity outcome.
  if [ "$BOOTSTRAP_KILLED" = 1 ]; then
    # The old cgroup is dead. Returning non-zero lets the ERR trap enter the
    # full rollback path; it must never thaw/revive a dead bootstrap runtime.
    log "EC2 bootstrap post-kill integrity failure: $*"
    return 1
  fi
  if ! restore_bootstrap_runtime; then
    log "ERROR: EC2 bootstrap could not fully restore the frozen old runtime: $*"
  fi
  log "EC2 bootstrap refused promotion: $*"
  return 1
}

restore_bootstrap_runtime() {
  local restore_failed=0 observed=0
  if [ "$BOOTSTRAP_ACTIVE" != 1 ]; then return; fi
  rm -f "$RUNTIME_DROPIN" || restore_failed=1
  systemctl daemon-reload || restore_failed=1
  # The frozen old operation may be waiting on one of these exact locks.
  # Restore its lock ownership before thawing Python execution.
  release_migration_lock_holder || restore_failed=1
  if [ -n "$BOOTSTRAP_CGROUP" ] && [ -w "$BOOTSTRAP_CGROUP/cgroup.freeze" ]; then
    printf '0\n' > "$BOOTSTRAP_CGROUP/cgroup.freeze" || restore_failed=1
    for _ in $(seq 1 20); do
      grep -qx 'frozen 0' "$BOOTSTRAP_CGROUP/cgroup.events" && { observed=1; break; }
      sleep 0.1
    done
    [ "$observed" = 1 ] || restore_failed=1
  else
    restore_failed=1
  fi
  if [ -n "$BOOTSTRAP_PID" ]; then
    kill -0 "$BOOTSTRAP_PID" 2>/dev/null || restore_failed=1
    systemctl is-active --quiet "$UNIT" || restore_failed=1
  else
    restore_failed=1
  fi
  [ "$restore_failed" = 0 ] && BOOTSTRAP_ACTIVE=0
  [ "$restore_failed" = 0 ]
}

restore_bootstrap_policy_after_kill() {
  local failed=0
  [ "$BOOTSTRAP_KILLED" = 1 ] || return 0
  rm -f "$RUNTIME_DROPIN" || failed=1
  systemctl reset-failed "$UNIT" || true
  systemctl daemon-reload || failed=1
  BOOTSTRAP_ACTIVE=0
  [ "$failed" = 0 ]
}

bootstrap_old_lock_census() {
  local key derived status
  local -a keys=(deployment)
  if ! derived=$(release_barrier_lock_keys); then
    bootstrap_fail "EC2 barrier could not derive record lock keys"
    return 1
  fi
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    keys+=("$key")
  done <<< "$derived"
  if start_migration_lock_holder all "${keys[@]}"; then return 0; else status=$?; fi
  if [ "$status" = 75 ]; then
    bootstrap_fail "EC2 deployment deferred: old migration lock contention"
  else
    bootstrap_fail "EC2 deployment integrity failure: migration lock inventory refused"
  fi
}

bootstrap_stop_old_orchestrator() {
  local control_group restart_after pid_after
  control_group=$(systemctl show "$UNIT" -p ControlGroup --value) \
    || fail "EC2 bootstrap could not resolve unit cgroup"
  [ -n "$control_group" ] && [ "$control_group" != / ] \
    || fail "EC2 bootstrap refused root/empty unit cgroup"
  BOOTSTRAP_CGROUP="${CGROUP_ROOT}${control_group}"
  [ -d "$BOOTSTRAP_CGROUP" ] && [ -w "$BOOTSTRAP_CGROUP/cgroup.freeze" ] && [ -w "$BOOTSTRAP_CGROUP/cgroup.kill" ] \
    || fail "EC2 bootstrap requires writable cgroup-v2 freeze/kill controls"
  BOOTSTRAP_PID=$(systemctl show "$UNIT" -p MainPID --value)
  [ "$BOOTSTRAP_PID" -gt 0 ] || fail "EC2 bootstrap found no live orchestrator PID"
  BOOTSTRAP_PROCS=$(cat "$BOOTSTRAP_CGROUP/cgroup.procs") || fail "EC2 bootstrap could not read cgroup membership"
  BOOTSTRAP_ACTIVE=1
  install -d -m 0755 "$RUNTIME_DROPIN_DIR"
  printf '[Service]\nRestart=no\n' > "$RUNTIME_DROPIN"
  systemctl daemon-reload
  restart_after=$(systemctl show "$UNIT" -p Restart --value)
  pid_after=$(systemctl show "$UNIT" -p MainPID --value)
  if [ "$restart_after" != no ] || [ "$pid_after" != "$BOOTSTRAP_PID" ]; then
    bootstrap_fail "EC2 bootstrap runtime Restart=no did not apply without PID change"
    return 1
  fi
  printf '1\n' > "$BOOTSTRAP_CGROUP/cgroup.freeze"
  local observed=0
  for _ in $(seq 1 20); do
    grep -qx 'frozen 1' "$BOOTSTRAP_CGROUP/cgroup.events" && { observed=1; break; }
    sleep 0.1
  done
  if [ "$observed" != 1 ]; then
    bootstrap_fail "EC2 bootstrap cgroup did not freeze"
    return 1
  fi
  if [ "$(systemctl show "$UNIT" -p MainPID --value)" != "$BOOTSTRAP_PID" ] \
    || [ "$(cat "$BOOTSTRAP_CGROUP/cgroup.procs")" != "$BOOTSTRAP_PROCS" ]; then
    bootstrap_fail "EC2 bootstrap PID/cgroup membership changed while frozen"
    return 1
  fi
  if ! bootstrap_old_lock_census; then return 1; fi
  if ! release_barrier_audit; then
    bootstrap_fail "EC2 deployment migration barrier integrity audit failed"
    return 1
  fi
  # Do not use systemctl stop: systemd v252 thaws before its stop kill path.
  printf '1\n' > "$BOOTSTRAP_CGROUP/cgroup.kill"
  BOOTSTRAP_KILLED=1
  observed=0
  for _ in $(seq 1 50); do
    if ! kill -0 "$BOOTSTRAP_PID" 2>/dev/null \
      && [ -z "$(cat "$BOOTSTRAP_CGROUP/cgroup.procs")" ] \
      && ! systemctl is-active --quiet "$UNIT"; then
      observed=1; break
    fi
    sleep 0.1
  done
  if [ "$observed" != 1 ]; then
    bootstrap_fail "EC2 bootstrap cgroup.kill did not converge to dead/inactive old runtime"
    return 1
  fi
  release_migration_lock_holder \
    || { bootstrap_fail "EC2 bootstrap lock holder did not exit cleanly"; return 1; }
  rm -f "$RUNTIME_DROPIN"
  systemctl reset-failed "$UNIT" || true
  systemctl daemon-reload
  BOOTSTRAP_ACTIVE=0
  BOOTSTRAP_STOPPED=1
}

enter_deployment_migration_barrier() {
  local live_contract lock_status
  live_contract=$(curl -fsS --max-time 5 -H "X-API-Key: $API_KEY" http://localhost:8000/api-surface 2>/dev/null \
    | EXPECTED_SHA="$ORIGINAL_SOURCE_SHA" /usr/bin/python3.11 -c 'import json,os,sys; d=json.load(sys.stdin); print(1 if d.get("source_sha")==os.environ["EXPECTED_SHA"] and d.get("contracts",{}).get("deployment_migration_barrier")==1 else 0)' \
    || echo 0)
  if [ "$live_contract" = 1 ]; then
    if start_migration_lock_holder named deployment; then lock_status=0; else lock_status=$?; fi
    if [ "$lock_status" = 75 ]; then
      fail "EC2 deployment deferred: active migration owns deployment barrier"
    elif [ "$lock_status" != 0 ]; then
      fail "EC2 deployment migration lock inventory is invalid"
    fi
    release_barrier_audit || fail "EC2 deployment migration barrier integrity audit failed"
  else
    bootstrap_stop_old_orchestrator
  fi
}

leave_deployment_migration_barrier() {
  restore_bootstrap_runtime
  release_migration_lock_holder
}

rollback() {
  trap - ERR INT TERM
  set +e
  log "rolling back code, images, and service"
  if [ "${LIVE_MOVED:-0}" = 1 ]; then
    systemctl stop "$UNIT" || true
    [ ! -d "$LIVE_DIR" ] || mv "$LIVE_DIR" "$FAILED_DIR"
    [ ! -d "$ROLLBACK_DIR" ] || mv "$ROLLBACK_DIR" "$LIVE_DIR"
  fi
  if [ "${DROPIN_CHANGED:-0}" = 1 ]; then
    if [ "${DROPIN_HAD_LIVE:-0}" = 1 ]; then
      cp -a "$DROPIN_BACKUP" "$RELEASE_DROPIN"
    else
      rm -f "$RELEASE_DROPIN"
    fi
    systemctl daemon-reload || true
  fi
  if [ "${IMAGES_PROMOTED:-0}" = 1 ]; then
    if [ "$CORE_HAD_LIVE" = 1 ]; then
      docker tag matrx-sandbox:rollback matrx-sandbox:latest
    else
      docker image rm matrx-sandbox:latest >/dev/null 2>&1 || true
    fi
    if [ "$SLIM_HAD_LIVE" = 1 ]; then
      docker tag matrx-sandbox:slim-rollback matrx-sandbox:slim
    else
      docker image rm matrx-sandbox:slim >/dev/null 2>&1 || true
    fi
    if [ "$DEVELOPMENT_HAD_LIVE" = 1 ]; then
      docker tag matrx-sandbox:development-rollback matrx-sandbox:development
    else
      docker image rm matrx-sandbox:development >/dev/null 2>&1 || true
    fi
  fi
  if [ "${HOME_HELPER_PROMOTED:-0}" = 1 ]; then
    if [ "${HOME_HELPER_HAD_LIVE:-0}" = 1 ]; then
      docker tag matrx-orchestrator:home-helper-rollback matrx-orchestrator:home-helper
    else
      docker image rm matrx-orchestrator:home-helper >/dev/null 2>&1 || true
    fi
  fi
  # Never leave the per-SHA candidates behind: a failed release that keeps
  # them is what filled the root volume and blocked the next three deploys.
  cleanup_candidate_images
  # Restore every bootstrap-only unit mutation before the one final old-unit
  # start.  Keep deployment EX ownership through exact rollback health.
  if [ "$BOOTSTRAP_KILLED" = 1 ]; then
    restore_bootstrap_policy_after_kill \
      || log "ERROR: rollback could not restore the old unit restart policy"
  else
    restore_bootstrap_runtime \
      || log "ERROR: rollback could not restore the live bootstrap runtime"
  fi
  systemctl start "$UNIT" || true
  rollback_verified=0
  if [ -n "$ORIGINAL_SOURCE_SHA" ]; then
    for _ in $(seq 1 30); do
      rollback_source=$(tr -d '[:space:]' < "$LIVE_DIR/.source-sha" 2>/dev/null || true)
      rollback_payload=$(curl -fsS --max-time 5 -H "X-API-Key: $API_KEY" http://localhost:8000/api-surface 2>/dev/null || true)
      if [ "$rollback_source" = "$ORIGINAL_SOURCE_SHA" ] \
        && RELEASE_PAYLOAD="$rollback_payload" EXPECTED_SHA="$ORIGINAL_SOURCE_SHA" /usr/bin/python3.11 - <<'PY'
import json, os
d=json.loads(os.environ["RELEASE_PAYLOAD"])
paths={r["path"] for r in d.get("routes", [])}
assert d.get("source_sha") == os.environ["EXPECTED_SHA"]
assert d.get("contracts", {}).get("filesystem") == 2
assert {"/sandboxes/{sandbox_id}/fs/{path:path}", "/sandboxes/{sandbox_id}/fs/watch"} <= paths
PY
      then rollback_verified=1; break; fi
      sleep 2
    done
  fi
  rollback_aidream_url=$(resolve_setting MATRX_AIDREAM_URL)
  [ "$rollback_aidream_url" = "$EXPECTED_AIDREAM_URL" ] \
    || log "ERROR: rollback EC2 route is '${rollback_aidream_url:-unset}', expected $EXPECTED_AIDREAM_URL"
  curl -fsS --max-time 15 "$EXPECTED_AIDREAM_URL/health/version" >/dev/null \
    || log "ERROR: rollback orchestrator cannot reach AWS-local AI Dream"
  [ "$rollback_verified" = 1 ] \
    || log "ERROR: rollback exact source/API/filesystem health did not recover"
  # Releasing admission is the only operation after the final old-unit start.
  release_migration_lock_holder
  # EXIT, do not return. This handler starts with `trap - ERR` + `set +e`, so
  # returning resumed the script right after the failing statement with -e
  # disabled: it walked into the success epilogue, deleted the failed-release
  # directory, logged "release … is healthy and exact" and exited 0. A
  # rolled-back release was reported to GitHub as a SUCCESSFUL deploy while
  # EC2 kept serving the old revision — observed on b7d131e, 2026-08-11.
  log "release $TARGET_SHA FAILED and was rolled back"
  exit 1
}
trap 'rollback' ERR INT TERM

log "promoting candidates"
# Freeze creates while the default + per-template tags move together.
enter_deployment_migration_barrier
# Keep the exact helper image reachable after release-candidate tag cleanup.
# This is a live tag mutation, so it happens only after the deployment barrier.
HOME_HELPER_HAD_LIVE=0
if docker image inspect matrx-orchestrator:home-helper >/dev/null 2>&1; then
  HOME_HELPER_HAD_LIVE=1
  docker tag matrx-orchestrator:home-helper matrx-orchestrator:home-helper-rollback
else
  docker image rm matrx-orchestrator:home-helper-rollback >/dev/null 2>&1 || true
fi
docker tag "$ECR_REPO-orchestrator:$TARGET_SHA" matrx-orchestrator:home-helper
HOME_HELPER_PROMOTED=1
if [ "$BOOTSTRAP_STOPPED" != 1 ]; then
  systemctl stop "$UNIT"
fi
CORE_HAD_LIVE=0
SLIM_HAD_LIVE=0
DEVELOPMENT_HAD_LIVE=0
if docker image inspect matrx-sandbox:latest >/dev/null 2>&1; then
  CORE_HAD_LIVE=1
  docker tag matrx-sandbox:latest matrx-sandbox:rollback
else
  docker image rm matrx-sandbox:rollback >/dev/null 2>&1 || true
fi
if docker image inspect matrx-sandbox:slim >/dev/null 2>&1; then
  SLIM_HAD_LIVE=1
  docker tag matrx-sandbox:slim matrx-sandbox:slim-rollback
else
  docker image rm matrx-sandbox:slim-rollback >/dev/null 2>&1 || true
fi
if docker image inspect matrx-sandbox:development >/dev/null 2>&1; then
  DEVELOPMENT_HAD_LIVE=1
  docker tag matrx-sandbox:development matrx-sandbox:development-rollback
else
  docker image rm matrx-sandbox:development-rollback >/dev/null 2>&1 || true
fi
IMAGES_PROMOTED=1
docker tag "$ECR_REPO:$TARGET_SHA" matrx-sandbox:latest
docker tag "$ECR_REPO:slim-$TARGET_SHA" matrx-sandbox:slim
docker tag "$ECR_REPO:development-$TARGET_SHA" matrx-sandbox:development

rm -rf "$ROLLBACK_DIR"
LIVE_MOVED=0
if [ -d "$LIVE_DIR" ]; then
  mv "$LIVE_DIR" "$ROLLBACK_DIR"
  LIVE_MOVED=1
fi
mv "$CANDIDATE_DIR" "$LIVE_DIR"
install -d "$DROPIN_DIR"
DROPIN_HAD_LIVE=0
if [ -r "$RELEASE_DROPIN" ]; then
  DROPIN_HAD_LIVE=1
  cp -a "$RELEASE_DROPIN" "$DROPIN_BACKUP"
fi
DROPIN_CHANGED=1
# `python -m uvicorn`, NOT `.venv/bin/uvicorn`. uv writes console scripts with
# a shebang pointing at the absolute path the venv was BUILT at
# (/home/ec2-user/orchestrator-candidate-<sha>/.venv/bin/python), and this
# release moves that directory to /home/ec2-user/orchestrator — so the script
# is unrunnable the moment it goes live: systemd reports 203/EXEC "Failed to
# execute .../.venv/bin/uvicorn: No such file or directory" and crashloops,
# the contract assertion below times out, and the release rolls back. That is
# what pinned EC2 on f229d4b (a pre-atomic-deploy revision) for weeks.
# `.venv/bin/python` is a symlink to /usr/bin/python3.11, so it survives the
# move and resolves the venv from its own pyvenv.cfg. Verified on the box:
# after mv, the console script exits 127 while `python -m uvicorn` runs.
cat > "$RELEASE_DROPIN" <<'EOF'
[Service]
WorkingDirectory=/home/ec2-user/orchestrator
BindPaths=/var/lib/matrx-sandbox/hosted-migrations
ExecStart=
ExecStart=/home/ec2-user/orchestrator/.venv/bin/python -m uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000 --workers 1
EOF
systemctl daemon-reload
systemctl start "$UNIT"

log "asserting exact source/API/filesystem contract"
verified=0
for _ in $(seq 1 30); do
  if payload=$(curl -fsS --max-time 5 -H "X-API-Key: $API_KEY" \
      http://localhost:8000/api-surface 2>/dev/null) \
      && RELEASE_PAYLOAD="$payload" EXPECTED_SHA="$TARGET_SHA" \
         /usr/bin/python3.11 - <<'PY'
import json, os
d = json.loads(os.environ["RELEASE_PAYLOAD"])
paths = {r["path"] for r in d.get("routes", [])}
required = {"/sandboxes/{sandbox_id}/fs/{path:path}", "/sandboxes/{sandbox_id}/fs/watch"}
assert d.get("source_sha") == os.environ["EXPECTED_SHA"]
assert d.get("contracts", {}).get("filesystem") == 2
assert d.get("contracts", {}).get("deployment_migration_barrier") == 1
assert required <= paths
PY
  then verified=1; break; fi
  sleep 2
done
if [ "$verified" != 1 ]; then
  # Say WHY before rolling back. This used to be a bare `|| false`, so a
  # release that never booted produced no diagnosis at all — the SSM log
  # jumped straight from "asserting contract" to "rolling back", and the
  # actual cause (systemd 203/EXEC on a relocated venv) was only visible in
  # the box's journal, which nobody reads when the workflow says success.
  echo "[deploy-ec2] contract assertion FAILED for $TARGET_SHA after 60s" >&2
  systemctl status "$UNIT" --no-pager -l 2>&1 | head -20 >&2
  journalctl -u "$UNIT" -n 30 --no-pager 2>&1 | tail -30 >&2
  echo "[deploy-ec2] last /api-surface payload: ${payload:-<no response>}" | head -c 2000 >&2
  echo >&2
  echo "[deploy-ec2] ERROR: release $TARGET_SHA did not come up healthy and exact" >&2
  # Call rollback directly — do NOT use fail() here. `exit` does not fire the
  # ERR trap, so fail() would leave the broken release LIVE and un-rolled-back.
  # rollback() restores the previous release and exits non-zero itself.
  rollback
fi

LIVE_AIDREAM_URL=$(resolve_setting MATRX_AIDREAM_URL)
if [ "$LIVE_AIDREAM_URL" != "$EXPECTED_AIDREAM_URL" ]; then
  echo "[deploy-ec2] ERROR: live EC2 orchestrator route changed during promotion: '${LIVE_AIDREAM_URL:-unset}'" >&2
  rollback
fi
if ! curl -fsS --max-time 15 "$LIVE_AIDREAM_URL/health/version" >/dev/null; then
  echo "[deploy-ec2] ERROR: live EC2 orchestrator cannot reach its AWS-local AI Dream replica" >&2
  rollback
fi

# Control-plane deployment does not authorize replacing user containers.
# Image migration requires separate lifecycle/persistence proof and approval,
# including for a development worker with a persistent home mount.
log "sandbox image migration NOT run; existing user containers are retained"

docker image rm matrx-orchestrator:home-helper-rollback >/dev/null 2>&1 || true
leave_deployment_migration_barrier

trap - ERR INT TERM
rm -rf "$FAILED_DIR" "$DROPIN_BACKUP"
docker image rm \
  "$ECR_REPO:$TARGET_SHA" \
  "$ECR_REPO:slim-$TARGET_SHA" \
  "$ECR_REPO:development-$TARGET_SHA" \
  "$ECR_REPO-orchestrator:$TARGET_SHA" >/dev/null 2>&1 || true
log "release $TARGET_SHA is healthy and exact"
