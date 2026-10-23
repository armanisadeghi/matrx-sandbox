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
