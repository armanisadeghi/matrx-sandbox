#!/usr/bin/env bash
# release.sh — bump the version, push it to main, tag it. Never refuses. (Arman, 2026-09-24)
#
# Usage (./ship.sh runs it after scripts/sync-main.py):
#   scripts/release.sh                     # patch bump
#   scripts/release.sh --minor | --major
#   scripts/release.sh --message "note"    # commit "release: vX.Y.Z - note"
#   scripts/release.sh --dry-run           # what would ship; nothing changes
#
# THE RULES (every release script in every repo):
#   1. It never denies a release. A dirty folder, a checkout on another branch, local commits
#      that conflict, an existing tag, a bad flag: each is a WARNING or ERROR line, and the
#      release goes out. The only stops: GitHub unreachable, the version unreadable, or five
#      lost push races in a row.
#   2. Before the push it only makes the release commit: the version bump (and the changelog
#      heading when this repo keeps one). Anything that checks the code runs AFTER the push
#      (AFTER_PUSH below) and can only produce findings.
#   3. It prints one line — "vX.Y.Z  pushed  (Ns)" — then, only if something is wrong, one
#      section per category of WARNING / ERROR rows. Never INFO. Full detail: the log file.
#
# The release commit is built with git plumbing on top of origin/main (a temporary index,
# commit-tree, push <sha>:main): the working folder is never read for it, never stashed,
# rebased or reset, so other sessions' uncommitted files can neither ride along nor block it.
#
# Canonical copy: matrx-ship/scripts/release-template.sh. Each repo keeps a copy with only the
# settings block changed; guard: matrx-ship/scripts/test-release-template.sh.

# ── the only per-repo settings ─────────────────────────────────────────────────
VERSION_FILE="package.json"   # JSON with "version": "x.y.z", a pyproject.toml, or a plain VERSION file
TAG_PREFIX="v"                # the tag is TAG_PREFIX + version
EXTRA_VERSION_FILES=("orchestrator/pyproject.toml" "sandbox-image/sdk/pyproject.toml")        # other files that carry the same version (JSON or pyproject.toml)
CHANGELOG=""                  # when set: a "## x.y.z - date" heading goes under "## Unreleased"
AFTER_PUSH=""                 # a command run after the push; a failure is an ERROR finding
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
REMOTE="origin"
BRANCH="main"
START=$SECONDS

LOG_DIR="${HOME}/.matrx/release-logs/$(basename "$ROOT")"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="${TMPDIR:-/tmp}"
LOG="$LOG_DIR/release-$(date +%Y-%m-%d_%H-%M-%S).log"
: > "$LOG"
ln -sfn "$(basename "$LOG")" "$LOG_DIR/latest.log" 2>/dev/null || true
q() { "$@" >>"$LOG" 2>&1; }

FINDINGS=()
finding() { FINDINGS+=("$1|$2|$3|${4:-}"); }   # LEVEL CATEGORY "text" ["remedy"]
print_findings() {
    [[ ${#FINDINGS[@]} -gt 0 ]] || return 0
    local row r cat seen="|" bar="====================" lvl c text remedy
    for row in "${FINDINGS[@]}"; do
        IFS='|' read -r _ cat _ _ <<< "$row"
        [[ "$seen" == *"|$cat|"* ]] && continue
        seen+="$cat|"
        echo ""
        echo "$bar $cat $bar"
        printf '%-8s %-12s %s\n' "LEVEL" "CATEGORY" "FINDING"
        for r in "${FINDINGS[@]}"; do
            IFS='|' read -r lvl c text remedy <<< "$r"
            [[ "$c" == "$cat" ]] || continue
            printf '%-8s %-12s %s%s\n' "$lvl" "$c" "$text" "${remedy:+  → $remedy}"
        done
        echo ""
        echo "$bar End of $cat $bar"
    done
}
stop() {   # the only way out before the push: nothing was released
    echo "release.sh: NOT RELEASED — $1 (log: $LOG)" >&2
    print_findings
    exit 1
}

# ── flags: a bad one is a WARNING, never a stop ──────────────────────────────
BUMP="patch"; NOTE=""; DRY=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --patch) BUMP="patch"; shift ;;
        --minor) BUMP="minor"; shift ;;
        --major) BUMP="major"; shift ;;
        --message|-m)
            if [[ -n "${2:-}" && "${2:-}" != --* ]]; then NOTE="$2"; shift 2
            else finding "WARNING" "Invocation" "--message had no text — released without a note"; shift; fi ;;
        --dry-run) DRY=true; shift ;;
        *) finding "WARNING" "Invocation" "Unknown flag '$1' was ignored" "--patch --minor --major --message --dry-run"; shift ;;
    esac
done

# ── fetch: the ONE thing that can stop a release before it starts ────────────
fetched=false
for _ in 1 2 3; do q git fetch "$REMOTE" "$BRANCH" && { fetched=true; break; }; sleep 2; done
$fetched || stop "cannot reach GitHub ($REMOTE/$BRANCH)"

# ── the version, bumped on a blob: python does the text work ────────────────
# bump_text KIND OLD NEW < text > text     KIND: json | pyproject | plain | changelog
bump_text() {
    python3 -c '
import re, sys, datetime
kind, old, new = sys.argv[1:4]
text = sys.stdin.read()
if kind == "json":
    text, n = re.subn(r"(\"version\"\s*:\s*\")[^\"]*\"", lambda m: m[1] + new + "\"", text, count=1)
elif kind == "pyproject":
    text, n = re.subn(r"(?m)^(version\s*=\s*\")[^\"]*\"", lambda m: m[1] + new + "\"", text, count=1)
elif kind == "plain":
    text, n = new + "\n", 1
else:  # changelog
    head = "## Unreleased\n"
    n = text.count(head)
    if n:
        text = text.replace(head, head + "\n## %s - %s\n" % (new, datetime.date.today().isoformat()), 1)
sys.stdout.write(text)
sys.exit(0 if n else 3)
' "$@"
}
kind_of() {
    case "$1" in *.json) echo json ;; *pyproject.toml) echo pyproject ;; *) echo plain ;; esac
}
read_version() {   # tree → current version
    local text
    text="$(git cat-file -p "$1:$VERSION_FILE" 2>/dev/null)" || { echo "0.0.0"; return 0; }
    python3 -c '
import json, re, sys
kind, text = sys.argv[1], sys.stdin.read()
if kind == "json":
    print(json.loads(text)["version"])
elif kind == "pyproject":
    print(re.search(r"(?m)^version\s*=\s*\"([^\"]+)\"", text)[1])
else:
    print(text.strip())
' "$(kind_of "$VERSION_FILE")" <<< "$text"
}
next_version() {   # current bump → new (pre-release counters bump on patch)
    python3 -c '
import re, sys
cur, bump = sys.argv[1:3]
m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$", cur)
if not m:
    sys.exit(1)
a, b, c, pre = int(m[1]), int(m[2]), int(m[3]), m[4]
if bump == "major":   print("%d.0.0" % (a + 1))
elif bump == "minor": print("%d.%d.0" % (a, b + 1))
elif pre and re.search(r"\d+$", pre):
    print("%d.%d.%d-%s" % (a, b, c, re.sub(r"\d+$", lambda n: str(int(n[0]) + 1), pre)))
else:                 print("%d.%d.%d" % (a, b, c + 1))
' "$1" "$2"
}

# ── the release tree: origin/main + this checkout's unpushed main commits ────
LOCAL_HEAD=""
if [[ "$(git symbolic-ref -q --short HEAD)" == "$BRANCH" ]]; then
    LOCAL_HEAD="$(git rev-parse HEAD)"
else
    finding "WARNING" "Git" "This checkout is not on $BRANCH — its commits were not included" "git checkout $BRANCH"
fi
base_tree() {   # sets BASE, BASE_TREE, PARENTS
    BASE="$(git rev-parse "$REMOTE/$BRANCH")"
    PARENTS=(-p "$BASE")
    BASE_TREE="$(git rev-parse "$BASE^{tree}")"
    [[ -z "$LOCAL_HEAD" ]] && return 0
    git merge-base --is-ancestor "$LOCAL_HEAD" "$BASE" && return 0
    local merged
    if merged="$(git merge-tree --write-tree "$BASE" "$LOCAL_HEAD" 2>/dev/null)"; then
        BASE_TREE="$merged"
        PARENTS=(-p "$BASE" -p "$LOCAL_HEAD")
    else
        finding "ERROR" "Git" "Local commits conflict with $REMOTE/$BRANCH — released $BRANCH without ${LOCAL_HEAD:0:9}" "./ship.sh (the sync sorts the conflict)"
        LOCAL_HEAD=""
    fi
}
tag_taken() {
    git rev-parse -q --verify "refs/tags/$1" >/dev/null && return 0
    grep -q "refs/tags/$1\$" <<< "$REMOTE_TAGS"
}
build_commit() {   # sets RELEASE_SHA from BASE_TREE, CURRENT, NEW, MSG
    local idx f blob kind
    idx="$(mktemp)"; rm -f "$idx"
    GIT_INDEX_FILE="$idx" git read-tree "$BASE_TREE" || return 1
    for f in "$VERSION_FILE" ${EXTRA_VERSION_FILES[@]+"${EXTRA_VERSION_FILES[@]}"}; do
        kind="$(kind_of "$f")"
        if ! git cat-file -e "$BASE_TREE:$f" 2>/dev/null; then
            if [[ "$f" == "$VERSION_FILE" && "$kind" == plain ]]; then
                blob="$(printf '%s\n' "$NEW" | git hash-object -w --stdin)"
            else
                finding "WARNING" "Version" "$f is not in the repository — its version was not bumped"
                continue
            fi
        elif ! blob="$(git cat-file -p "$BASE_TREE:$f" | bump_text "$kind" "$CURRENT" "$NEW" | git hash-object -w --stdin)"; then
            finding "WARNING" "Version" "$f has no version field to bump — left unchanged"
            continue
        fi
        GIT_INDEX_FILE="$idx" git update-index --add --cacheinfo "100644,$blob,$f" || return 1
    done
    if [[ -n "$CHANGELOG" ]] && git cat-file -e "$BASE_TREE:$CHANGELOG" 2>/dev/null; then
        if blob="$(git cat-file -p "$BASE_TREE:$CHANGELOG" | bump_text changelog "$CURRENT" "$NEW" | git hash-object -w --stdin)"; then
            GIT_INDEX_FILE="$idx" git update-index --cacheinfo "100644,$blob,$CHANGELOG" || return 1
        else
            finding "WARNING" "Version" "$CHANGELOG has no '## Unreleased' heading — no release heading was added"
        fi
    fi
    local tree
    tree="$(GIT_INDEX_FILE="$idx" git write-tree)" || return 1
    rm -f "$idx"
    RELEASE_SHA="$(git commit-tree "$tree" "${PARENTS[@]}" -m "$MSG")"
}

base_tree
REMOTE_TAGS="$(git ls-remote --tags "$REMOTE" 2>/dev/null)"
if $DRY; then
    CURRENT="$(read_version "$BASE_TREE")" || stop "cannot read the version in $VERSION_FILE"
    NEW="$(next_version "$CURRENT" "$BUMP")" || stop "cannot bump the version '$CURRENT'"
    echo "release.sh: DRY RUN — $REMOTE/$BRANCH is at $CURRENT; would release ${TAG_PREFIX}${NEW}. Nothing changed."
    print_findings
    exit 0
fi

RACES=0; BLIPS=0; PUSHED=false
while (( RACES < 5 && BLIPS < 10 )); do
    CURRENT="$(read_version "$BASE_TREE")" || stop "cannot read the version in $VERSION_FILE"
    NEW="$(next_version "$CURRENT" "$BUMP")" || stop "cannot bump the version '$CURRENT' in $VERSION_FILE"
    while tag_taken "${TAG_PREFIX}${NEW}"; do NEW="$(next_version "$NEW" patch)"; done
    TAG="${TAG_PREFIX}${NEW}"
    MSG="release: ${TAG}${NOTE:+ - $NOTE}"
    build_commit || stop "could not assemble the release commit"
    [[ -n "${RELEASE_TEST_BEFORE_PUSH:-}" ]] && { bash -c "$RELEASE_TEST_BEFORE_PUSH" >/dev/null 2>&1 || true; }
    if q git push "$REMOTE" "$RELEASE_SHA:refs/heads/$BRANCH"; then PUSHED=true; break; fi
    seen="$(git rev-parse "$REMOTE/$BRANCH")"
    if q git fetch "$REMOTE" "$BRANCH" && [[ "$(git rev-parse "$REMOTE/$BRANCH")" != "$seen" ]]; then
        RACES=$((RACES + 1))
    else
        BLIPS=$((BLIPS + 1)); sleep $((BLIPS * 3))
    fi
    REMOTE_TAGS="$(git ls-remote --tags "$REMOTE" 2>/dev/null || echo "$REMOTE_TAGS")"
    base_tree
done
$PUSHED || stop "the push to GitHub failed ($RACES lost races, $BLIPS network failures)"

# ── from here the release is out; nothing below can fail it ──────────────────
if ! q git tag "$TAG" "$RELEASE_SHA" || ! q git push "$REMOTE" "refs/tags/$TAG"; then
    finding "ERROR" "Git" "Tag $TAG did not reach GitHub" "git push $REMOTE $TAG"
fi
if [[ "$(git symbolic-ref -q --short HEAD)" == "$BRANCH" ]]; then
    q git merge --ff-only "$REMOTE/$BRANCH" \
        || finding "WARNING" "Git" "This checkout could not fast-forward to $TAG — pull when convenient" "git pull --no-rebase $REMOTE $BRANCH"
fi
echo "$TAG  pushed  ($((SECONDS - START))s)"

if [[ -n "$AFTER_PUSH" ]]; then
    if ! (cd "$ROOT" && bash -c "$AFTER_PUSH") >>"$LOG" 2>&1; then
        finding "ERROR" "Checks" "After-push checks failed — the release is out; see $LOG" "$AFTER_PUSH"
    fi
fi
print_findings
exit 0
