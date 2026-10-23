#!/usr/bin/env bash
# ship.sh — sync this checkout with GitHub, then release. (Arman, 2026-09-24)
#
#   1. scripts/sync-main.py   commits everything uncommitted ("local work not committed by agents
#                             who made them"), merges origin/main, sorts every conflict into
#                             _conflicts/ (auto-fixed / both-versions-kept / held), pushes.
#   2. the release script     scripts/release.sh (or ./release.sh): bumps the version and ships.
#                             Runs whatever happened in step 1. If this repo has no release
#                             script, ship.sh says so and stops after the sync.
#   3. the open items         prints what is open in _conflicts/README.md, if anything, for the
#                             agent that resolves conflicts.
#
# Usage:
#   ./ship.sh                                   # sync + release with the default note
#   ./ship.sh "Added new chat surface"          # sync + release with a note
#   ./ship.sh "note" --minor                    # release flags pass through
#   ./ship.sh "note" --dry-run                  # NO sync; release --dry-run only
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# THE RELEASE PATH IS NEVER BLOCKED (Arman 2026-10-02): the git hooks this run's pushes trigger REPORT
# their failures (banner + agent mail) instead of refusing the push.
export MATRX_RELEASE_PATH=1
export RELEASE_STAGE_CALLER_PWD="$PWD"

NOTE="sync and release"
if [[ $# -gt 0 && "$1" != --* ]]; then
    NOTE="$1"
    shift
fi

DRY_RUN=false
for arg in "$@"; do [[ "$arg" == "--dry-run" ]] && DRY_RUN=true; done

RELEASE=""
for candidate in "$ROOT/scripts/release.sh" "$ROOT/release.sh"; do
    [[ -x "$candidate" || -f "$candidate" ]] && { RELEASE="$candidate"; break; }
done
# NESTED in the aidream monorepo (infra/matrx-sandbox, 2026-10-23): the sync below runs aidream's own
# sync (scripts/sync-main.py delegates) and the sandbox has no release of its own there (its v* tags
# moved to sandbox/v*; the deploy is the Sandbox Deploy workflow on aidream main), so nothing is released.
if [[ -n "$(git -C "$ROOT" rev-parse --show-prefix 2>/dev/null)" ]]; then
    echo "ship.sh: matrx-sandbox is a folder of $(git -C "$ROOT" rev-parse --show-toplevel): synced with that checkout's sync; no release of its own."
    RELEASE=""
fi

# ── 1. sync ──────────────────────────────────────────────────────────────────
if $DRY_RUN; then
    echo "ship.sh: --dry-run, so the sync was skipped (it commits and pushes for real)."
    SYNC_RC=0
else
    python3 "$ROOT/scripts/sync-main.py"
    SYNC_RC=$?
    if [[ $SYNC_RC -ne 0 ]]; then
        echo ""
        echo "ship.sh: the sync did not finish (exit $SYNC_RC; its reason is printed above). Releasing anyway."
    fi
fi

# ── 2. release ───────────────────────────────────────────────────────────────
echo ""
if [[ -n "$RELEASE" ]]; then
    bash "$RELEASE" --message "$NOTE" "$@"
    RELEASE_RC=$?
else
    echo "ship.sh: THIS REPO HAS NO RELEASE SCRIPT (looked for scripts/release.sh and ./release.sh)."
    $DRY_RUN || echo "ship.sh: the sync ran; nothing was released."
    RELEASE_RC=0
fi

# ── 3. open items ────────────────────────────────────────────────────────────
echo ""
if ! $DRY_RUN; then
    if python3 "$ROOT/scripts/check-conflict-markers.py" >/tmp/ship-conflicts.$$ 2>&1; then
        echo "ship.sh: nothing open in _conflicts/."
    else
        echo "ship.sh: open items in _conflicts/README.md:"
        sed 's/^/  /' /tmp/ship-conflicts.$$
    fi
    rm -f /tmp/ship-conflicts.$$
fi

echo "ship.sh: sync exit $SYNC_RC, release exit $RELEASE_RC"
exit $RELEASE_RC
