#!/usr/bin/env bash
#
# sync-internal-development-repos.sh
#
# On-demand helper to safely fast-forward the internal development
# repositories to their origin/main. This script is intentionally
# conservative: it will only fast-forward a repo's main branch when
# it is 100% safe to do so, and will refuse (not force) otherwise.
#
# It NEVER runs: git reset, git clean, git stash, or any force-push,
# and it never installs or creates any cron/schedule.
#
# Usage:
#   ./scripts/sync-internal-development-repos.sh
#   MATRX_REPOS_ROOT=/some/other/path ./scripts/sync-internal-development-repos.sh
#
# Exit status:
#   0  - all repos were already up to date or safely fast-forwarded
#   1  - one or more repos could not be safely updated (see summary)

set -euo pipefail

MATRX_REPOS_ROOT="${MATRX_REPOS_ROOT:-/home/agent/repos}"

REPOS=(
  "aidream"
  "common-docs"
  "matrx-extend"
  "matrx-frontend"
  "matrx-local"
  "matrx-sandbox"
  "matrx-ship"
)

overall_status=0

# Columns for summary
printf '%-24s %-10s %s\n' "REPO" "RESULT" "DETAIL"
printf -- '------------------------ ---------- ------\n'

for repo in "${REPOS[@]}"; do
  repo_path="${MATRX_REPOS_ROOT}/${repo}"

  if [[ ! -d "${repo_path}" ]]; then
    printf '%-24s %-10s %s\n' "${repo}" "SKIP" "directory not found: ${repo_path}"
    overall_status=1
    continue
  fi

  if [[ ! -d "${repo_path}/.git" ]]; then
    printf '%-24s %-10s %s\n' "${repo}" "SKIP" "not a git repository"
    overall_status=1
    continue
  fi

  # Run each repo's checks in a subshell so `cd` and any failure is contained.
  if ! result=$(
    set -euo pipefail
    cd "${repo_path}"

    # Refuse if dirty (unstaged, staged, or untracked changes).
    if [[ -n "$(git status --porcelain)" ]]; then
      echo "REFUSE|working tree is dirty"
      exit 0
    fi

    # Refuse if detached HEAD.
    current_branch="$(git symbolic-ref --quiet --short HEAD || true)"
    if [[ -z "${current_branch}" ]]; then
      echo "REFUSE|HEAD is detached"
      exit 0
    fi

    # Refuse if not on main.
    if [[ "${current_branch}" != "main" ]]; then
      echo "REFUSE|on branch '${current_branch}', not main"
      exit 0
    fi

    # Fetch latest main from origin (read-only, no local refs touched yet).
    if ! git fetch origin main --quiet; then
      echo "REFUSE|failed to fetch origin main"
      exit 0
    fi

    local_sha="$(git rev-parse main)"
    remote_sha="$(git rev-parse FETCH_HEAD)"

    if [[ "${local_sha}" == "${remote_sha}" ]]; then
      echo "OK|already up to date at ${local_sha:0:7}"
      exit 0
    fi

    # Determine ancestry: only fast-forward if local main is a strict
    # ancestor of origin/main (i.e. no local commits diverge from it).
    if git merge-base --is-ancestor "${local_sha}" "${remote_sha}"; then
      git merge --ff-only FETCH_HEAD --quiet
      new_sha="$(git rev-parse main)"
      echo "UPDATED|${local_sha:0:7} -> ${new_sha:0:7}"
      exit 0
    fi

    # Local main has diverged from origin/main (local-only commits, or
    # a genuine divergence) - refuse rather than modify anything.
    echo "REFUSE|diverged from origin/main (local=${local_sha:0:7} remote=${remote_sha:0:7})"
    exit 0
  ); then
    printf '%-24s %-10s %s\n' "${repo}" "ERROR" "unexpected failure running checks"
    overall_status=1
    continue
  fi

  status="${result%%|*}"
  detail="${result#*|}"

  case "${status}" in
    OK)
      printf '%-24s %-10s %s\n' "${repo}" "OK" "${detail}"
      ;;
    UPDATED)
      printf '%-24s %-10s %s\n' "${repo}" "UPDATED" "${detail}"
      ;;
    REFUSE)
      printf '%-24s %-10s %s\n' "${repo}" "REFUSED" "${detail}"
      overall_status=1
      ;;
    *)
      printf '%-24s %-10s %s\n' "${repo}" "ERROR" "unrecognized result: ${result}"
      overall_status=1
      ;;
  esac
done

exit "${overall_status}"
