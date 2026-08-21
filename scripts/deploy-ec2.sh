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
AIDREAM_URL=$(resolve_setting MATRX_AIDREAM_URL)
EXPECTED_AIDREAM_URL="http://aidream.internal.matrxserver.com"
[ -n "$DB_URL" ] || fail "MATRX_DATABASE_URL is unresolved; migrations may not be skipped"
[ -n "$API_KEY" ] || fail "MATRX_API_KEY is unresolved; production metadata must stay authenticated"
# Pre-flight for the store guard in orchestrator/config.py: the new container
# would refuse to boot on anything but 'postgres' here. Catch it BEFORE the
# swap instead of after.
[ "$STORE" = "postgres" ] \
  || fail "MATRX_SANDBOX_STORE is '${STORE:-unset}'; a deployed orchestrator must set it to 'postgres' (in-memory loses every sandbox row on restart)"
# EC2-origin sandbox traffic must stay on the co-located sandbox_host replica.
# Falling back to the public Coolify app_server defeats the two-runtime topology
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

log "applying required migrations before promotion"
validate_release_authority
sudo -u ec2-user env MATRX_DATABASE_URL="$DB_URL" \
  "$CANDIDATE_DIR/.venv/bin/python" -m orchestrator.migrate_runner
validate_release_authority

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
  systemctl start "$UNIT" || true
  # Never leave the per-SHA candidates behind: a failed release that keeps
  # them is what filled the root volume and blocked the next three deploys.
  cleanup_candidate_images
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
systemctl stop "$UNIT"
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

# A successful release must also move persistent internal development workers
# onto the approved image. Their /home/agent is a host-mounted EBS workspace,
# so the zero-drift swap preserves repository data byte-for-byte. Ordinary EC2
# sandboxes remain safely unsupported/deferred by migrate-all's storage and
# activity gates. This is deploy-triggered, not a polling schedule.
log "migrating idle persistent development workers to the approved image"
if MIGRATION_RESULT=$(curl -fsS --max-time 240 -X POST \
    -H "X-API-Key: $API_KEY" \
    http://localhost:8000/migrate-all); then
  MIGRATION_RESULT="$MIGRATION_RESULT" /usr/bin/python3.11 - <<'PY' || true
import json
import os

result = json.loads(os.environ["MIGRATION_RESULT"])
if result.get("failed"):
    print("[deploy-ec2] WARNING: sandbox image migration failures:", result["failed"])
print("[deploy-ec2] migration result:", json.dumps(result, sort_keys=True))
PY
else
  log "WARNING: post-release migration trigger failed; development workers retry on their next AI connection"
fi

trap - ERR INT TERM
rm -rf "$FAILED_DIR" "$DROPIN_BACKUP"
docker image rm \
  "$ECR_REPO:$TARGET_SHA" \
  "$ECR_REPO:slim-$TARGET_SHA" \
  "$ECR_REPO:development-$TARGET_SHA" \
  "$ECR_REPO-orchestrator:$TARGET_SHA" >/dev/null 2>&1 || true
log "release $TARGET_SHA is healthy and exact"
