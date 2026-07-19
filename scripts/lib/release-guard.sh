#!/usr/bin/env bash
# Shared fail-closed release ancestry checks. Callers must define fail().

release_guard_validate_sha() {
  local sha="$1" label="$2"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail "$label must be a full commit SHA"
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

release_guard_assert_descendant() {
  local repo="$1" deployed="$2" target="$3" label="$4"
  release_guard_validate_sha "$deployed" "$label"
  git -C "$repo" cat-file -e "${deployed}^{commit}" 2>/dev/null \
    || fail "$label $deployed is unavailable in release history"
  git -C "$repo" merge-base --is-ancestor "$deployed" "$target" \
    || fail "refusing downgrade/divergence: $target does not descend from $label $deployed"
}
