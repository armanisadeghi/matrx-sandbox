#!/usr/bin/env bash
# Shared fail-closed release ancestry checks. Callers must define fail().

# ── Release source layout ───────────────────────────────────────────────────
# The sandbox ships from its own repository today and from the aidream
# monorepo (infra/matrx-sandbox) after the move. Every deploy path (the Deploy
# workflow, deploy-ec2.sh, deploy-hosted.sh) derives the release ref names from
# the checkout's own layout HERE, so the move flips no value in the scripts:
#   root   (git prefix empty)  refs/heads/deploy/hosted
#                              refs/tags/deploy-approved/<sha>
#   nested (inside a monorepo) refs/heads/deploy/sandbox-hosted
#                              refs/tags/sandbox/deploy-approved/<sha>
# MATRX_SANDBOX_DEPLOY_REF / MATRX_SANDBOX_APPROVAL_PREFIX override (validated).
# The host poller (scripts/systemd/pull-deploy-runner.sh) carries the same
# rule because it must pick the ref before it has a checkout; a test pins both.
release_source_init() {
  local root="$1" prefix
  prefix=$(git -C "$root" rev-parse --show-prefix 2>/dev/null) \
    || fail "cannot resolve the release checkout layout at $root"
  prefix="${prefix%/}"
  if [ -n "${MATRX_SANDBOX_SUBDIR:-}" ] && [ "${MATRX_SANDBOX_SUBDIR%/}" != "$prefix" ]; then
    fail "MATRX_SANDBOX_SUBDIR=$MATRX_SANDBOX_SUBDIR but $root sits at '${prefix:-<repo root>}'"
  fi
  MATRX_SANDBOX_SUBDIR="$prefix"
  if [ -z "$prefix" ]; then
    : "${MATRX_SANDBOX_DEPLOY_REF:=refs/heads/deploy/hosted}"
    : "${MATRX_SANDBOX_APPROVAL_PREFIX:=refs/tags/deploy-approved/}"
  else
    : "${MATRX_SANDBOX_DEPLOY_REF:=refs/heads/deploy/sandbox-hosted}"
    : "${MATRX_SANDBOX_APPROVAL_PREFIX:=refs/tags/sandbox/deploy-approved/}"
  fi
  [[ "$MATRX_SANDBOX_DEPLOY_REF" =~ ^refs/heads/[A-Za-z0-9._/-]+$ ]] \
    || fail "MATRX_SANDBOX_DEPLOY_REF must be a refs/heads/ name: $MATRX_SANDBOX_DEPLOY_REF"
  [[ "$MATRX_SANDBOX_APPROVAL_PREFIX" =~ ^refs/tags/([A-Za-z0-9._-]+/)+$ ]] \
    || fail "MATRX_SANDBOX_APPROVAL_PREFIX must be a refs/tags/.../ prefix: $MATRX_SANDBOX_APPROVAL_PREFIX"
  export MATRX_SANDBOX_SUBDIR MATRX_SANDBOX_DEPLOY_REF MATRX_SANDBOX_APPROVAL_PREFIX
}

# A history-rewriting import (git filter-repo) gives every pre-move commit a
# new SHA, while the live hosts still record the OLD one (EC2 .source-sha, the
# hosted state file, image labels). scripts/release-lineage.tsv — written once
# by the import, `old<TAB>new` per line, part of the approved tree — maps an
# old SHA that is absent from history to its rewritten twin. A SHA that exists,
# or that the map does not name exactly once, is returned unchanged so the
# caller's own check still fails closed.
release_guard_resolve_lineage() {
  local repo="$1" sha="$2" file mapped
  file="$repo/scripts/release-lineage.tsv"
  if git -C "$repo" cat-file -e "${sha}^{commit}" 2>/dev/null || [ ! -r "$file" ]; then
    printf '%s\n' "$sha"
    return 0
  fi
  mapped=$(awk -F '\t' -v s="$sha" '$1 == s {print $2; n++} END {exit n == 1 ? 0 : 1}' "$file") \
    && [[ "$mapped" =~ ^[0-9a-f]{40}$ ]] \
    && git -C "$repo" cat-file -e "${mapped}^{commit}" 2>/dev/null \
    || { printf '%s\n' "$sha"; return 0; }
  echo "[release-guard] $sha predates the monorepo import; lineage maps it to $mapped" >&2
  printf '%s\n' "$mapped"
}

release_guard_validate_sha() {
  local sha="$1" label="$2"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail "$label must be a full commit SHA"
}

# The atomic EC2 deploy replaced a legacy copy-in-place checkout that had no
# revision marker.  Permit that one transition only when the complete live
# source tree is byte-for-byte and mode-for-mode identical to the one known
# pre-marker release. Runtime-only state is excluded from the tree identity;
# none of it is executed by the candidate release.
release_guard_bootstrap_legacy_source() {
  local repo="$1" live_dir="$2" legacy_shas="$3" subtree="$4"
  local legacy_sha expected_tree actual_tree verify_root actual_root index_dir marker_tmp matched_sha=""

  [ -d "$live_dir" ] || fail "legacy live directory is missing: $live_dir"
  [ ! -e "$live_dir/.source-sha" ] && [ ! -L "$live_dir/.source-sha" ] \
    || fail "legacy bootstrap requires a missing source revision marker"
  verify_root=$(mktemp -d) || fail "cannot create legacy verification workspace"
  actual_root="$verify_root/actual"
  index_dir="$verify_root/index.git"
  mkdir "$actual_root" || {
    rm -rf "$verify_root"
    fail "cannot prepare legacy verification workspace"
  }
  if ! cp -a "$live_dir/." "$actual_root/"; then
    rm -rf "$verify_root"
    fail "cannot snapshot legacy live directory"
  fi

  # These are the only artifacts the old copy-in-place deployment could
  # legitimately mutate or generate alongside the repository source.
  rm -rf \
    "$actual_root/.venv" \
    "$actual_root/.pytest_cache" \
    "$actual_root/.coverage"
  rm -f "$actual_root/.env"
  find "$actual_root" -type d -name __pycache__ -prune -exec rm -rf {} +
  find "$actual_root" -maxdepth 1 -type d -name '*.egg-info' -exec rm -rf {} +

  if [ -e "$actual_root/.git" ]; then
    rm -rf "$verify_root"
    fail "legacy live directory has an ambiguous embedded Git checkout"
  fi
  if ! git init --bare --quiet "$index_dir" \
      || ! git --git-dir="$index_dir" --work-tree="$actual_root" add -A \
      || ! actual_tree=$(git --git-dir="$index_dir" write-tree); then
    rm -rf "$verify_root"
    fail "cannot compute legacy live source identity"
  fi
  rm -rf "$verify_root"

  # The copy-in-place workflow ran at more than one independently known
  # production revision before source markers became mandatory. Accept only an
  # exact Git-tree identity from this explicit allowlist and stamp the revision
  # that actually matched; never infer provenance from a partial file set.
  for legacy_sha in $legacy_shas; do
    release_guard_validate_sha "$legacy_sha" "known legacy source"
    legacy_sha=$(release_guard_resolve_lineage "$repo" "$legacy_sha")
    # `./` resolves the subtree from $repo (the sandbox root, nested or not);
    # the plain form is the toplevel path of a pre-move commit kept verbatim.
    expected_tree=$(git -C "$repo" rev-parse "${legacy_sha}:./${subtree}" 2>/dev/null \
      || git -C "$repo" rev-parse "${legacy_sha}:${subtree}" 2>/dev/null) \
      || fail "known legacy source $legacy_sha is unavailable"
    if [ "$actual_tree" = "$expected_tree" ]; then
      matched_sha="$legacy_sha"
      break
    fi
  done
  [ -n "$matched_sha" ] \
    || fail "unversioned live orchestrator does not match any known legacy source"

  marker_tmp=$(mktemp "$live_dir/.source-sha.tmp.XXXXXX") \
    || fail "cannot create legacy source revision marker"
  if ! printf '%s\n' "$matched_sha" > "$marker_tmp" \
      || ! chmod 0644 "$marker_tmp" \
      || ! mv -f "$marker_tmp" "$live_dir/.source-sha"; then
    rm -f "$marker_tmp"
    fail "cannot install legacy source revision marker"
  fi
}

release_guard_fetch_current_main() {
  local repo="$1" target="$2" current
  git -C "$repo" fetch origin refs/heads/main --quiet \
    || fail "cannot resolve current origin/main before release"
  current=$(git -C "$repo" rev-parse FETCH_HEAD 2>/dev/null) \
    || fail "cannot parse current origin/main"
  [ "$current" = "$target" ] \
    || fail "release target $target is not current origin/main $current"
}

release_guard_fetch_approved_release() {
  local repo="$1" target="$2" approved approval_ref
  release_guard_validate_sha "$target" "release target"
  release_source_init "$repo"
  approval_ref="${MATRX_SANDBOX_APPROVAL_PREFIX}$target"
  git -C "$repo" fetch origin "$approval_ref" --quiet \
    || fail "cannot resolve immutable approval ref $approval_ref"
  approved=$(git -C "$repo" rev-parse FETCH_HEAD 2>/dev/null) \
    || fail "cannot parse immutable approval ref $approval_ref"
  [ "$approved" = "$target" ] \
    || fail "approval ref $approval_ref resolves to $approved, not $target"
}

release_guard_assert_descendant() {
  local repo="$1" deployed="$2" target="$3" label="$4"
  release_guard_validate_sha "$deployed" "$label"
  deployed=$(release_guard_resolve_lineage "$repo" "$deployed")
  git -C "$repo" cat-file -e "${deployed}^{commit}" 2>/dev/null \
    || fail "$label $deployed is unavailable in release history"
  git -C "$repo" merge-base --is-ancestor "$deployed" "$target" \
    || fail "refusing downgrade/divergence: $target does not descend from $label $deployed"
}
