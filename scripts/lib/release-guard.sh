#!/usr/bin/env bash
# Shared fail-closed release ancestry checks. Callers must define fail().

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
  local repo="$1" live_dir="$2" legacy_sha="$3" subtree="$4"
  local expected_tree actual_tree verify_root actual_root index_dir marker_tmp

  release_guard_validate_sha "$legacy_sha" "known legacy source"
  [ -d "$live_dir" ] || fail "legacy live directory is missing: $live_dir"
  [ ! -e "$live_dir/.source-sha" ] \
    || fail "legacy bootstrap requires a missing source revision marker"
  expected_tree=$(git -C "$repo" rev-parse "${legacy_sha}:${subtree}" 2>/dev/null) \
    || fail "known legacy source $legacy_sha is unavailable"

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
  [ "$actual_tree" = "$expected_tree" ] \
    || fail "unversioned live orchestrator does not match the known legacy source"

  marker_tmp=$(mktemp "$live_dir/.source-sha.tmp.XXXXXX") \
    || fail "cannot create legacy source revision marker"
  if ! printf '%s\n' "$legacy_sha" > "$marker_tmp" \
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
  approval_ref="refs/tags/deploy-approved/$target"
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
  git -C "$repo" cat-file -e "${deployed}^{commit}" 2>/dev/null \
    || fail "$label $deployed is unavailable in release history"
  git -C "$repo" merge-base --is-ancestor "$deployed" "$target" \
    || fail "refusing downgrade/divergence: $target does not descend from $label $deployed"
}
