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
# The final copy-in-place release immediately before atomic, revision-stamped
# deployments. This is intentionally a single immutable source identity.
LEGACY_EC2_SOURCE_SHA=30ed118b431b72e8f73f1b199fd9398d78361ed5

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

# Migrations are forward-only. Re-check the moving branch and deployed code at
# every irreversible boundary so a workflow that becomes historical while it
# builds cannot roll the service back after newer work reaches main.
validate_release_authority() {
  release_guard_fetch_current_main "$RELEASE_ROOT" "$TARGET_SHA"
  if [ -d "$LIVE_DIR" ]; then
    if [ ! -e "$LIVE_DIR/.source-sha" ]; then
      log "verifying one-time legacy live-source bootstrap"
      release_guard_bootstrap_legacy_source \
        "$RELEASE_ROOT" "$LIVE_DIR" "$LEGACY_EC2_SOURCE_SHA" orchestrator
    fi
    [ -r "$LIVE_DIR/.source-sha" ] \
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
[ -n "$DB_URL" ] || fail "MATRX_DATABASE_URL is unresolved; migrations may not be skipped"
[ -n "$API_KEY" ] || fail "MATRX_API_KEY is unresolved; production metadata must stay authenticated"

log "pulling immutable image candidates"
docker pull "$ECR_REPO:$TARGET_SHA"
docker pull "$ECR_REPO:slim-$TARGET_SHA"
docker pull "$ECR_REPO-orchestrator:$TARGET_SHA"
for image in "$ECR_REPO:$TARGET_SHA" "$ECR_REPO:slim-$TARGET_SHA"; do
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
  fi
  systemctl start "$UNIT" || true
}
trap 'rollback' ERR INT TERM

log "promoting candidates"
# Freeze creates while the default + per-template tags move together.
systemctl stop "$UNIT"
CORE_HAD_LIVE=0
SLIM_HAD_LIVE=0
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
IMAGES_PROMOTED=1
docker tag "$ECR_REPO:$TARGET_SHA" matrx-sandbox:latest
docker tag "$ECR_REPO:slim-$TARGET_SHA" matrx-sandbox:slim

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
cat > "$RELEASE_DROPIN" <<'EOF'
[Service]
WorkingDirectory=/home/ec2-user/orchestrator
ExecStart=
ExecStart=/home/ec2-user/orchestrator/.venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000 --workers 1
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
[ "$verified" = 1 ] || false

trap - ERR INT TERM
rm -rf "$FAILED_DIR" "$DROPIN_BACKUP"
docker image rm \
  "$ECR_REPO:$TARGET_SHA" \
  "$ECR_REPO:slim-$TARGET_SHA" \
  "$ECR_REPO-orchestrator:$TARGET_SHA" >/dev/null 2>&1 || true
log "release $TARGET_SHA is healthy and exact"
