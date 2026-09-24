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
#      that conflict, an existing tag, a bad flag, a git hook: each is a WARNING or ERROR line,
#      and the release goes out. The only stops: GitHub unreachable, the version unreadable, or
#      five lost push races in a row.
#   2. Before the push it only makes the release commit: the version bump (and the changelog
#      heading when this repo keeps one). Anything that checks the code runs AFTER the push
#      (AFTER_PUSH below) and can only produce findings.
#   3. It prints one line — "vX.Y.Z  pushed  (Ns)" — then, only if something is wrong, one
#      section per category of WARNING / ERROR rows. Never INFO. Full detail: the log file.
#
# The release commit is built with git plumbing on top of origin/main (a temporary index,
# commit-tree, push <sha>:main): the working folder is never read for it, never stashed,
# rebased or reset, so other sessions' uncommitted files can neither ride along nor block it.
# No git hook runs: the push is --no-verify and the fast-forward runs with hooks off.
#
# Canonical copy: matrx-ship/scripts/release-template.sh. Each repo keeps a copy with only the
# settings block changed; guard: matrx-ship/scripts/test-release-template.sh.

# ── the only per-repo settings ─────────────────────────────────────────────────
VERSION_FILE="package.json"   # JSON with a top-level "version", a pyproject.toml, or a plain VERSION file
TAG_PREFIX="v"                # the tag is TAG_PREFIX + version
EXTRA_VERSION_FILES=("orchestrator/pyproject.toml" "sandbox-image/sdk/pyproject.toml")        # other JSON / pyproject.toml files that carry the same version
CHANGELOG=""                  # when set: a "## x.y.z - date" heading goes under "## Unreleased"
AFTER_PUSH=""                 # a command run after the push; a failure is an ERROR finding
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
REMOTE="origin"
BRANCH="main"
START=$SECONDS

LOG_DIR="${HOME:-${TMPDIR:-/tmp}}/.matrx/release-logs/$(basename "$ROOT")"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="${TMPDIR:-/tmp}"
LOG="$LOG_DIR/release-$(date +%Y-%m-%d_%H-%M-%S)-$$.log"
{ : > "$LOG"; } 2>/dev/null || LOG="$(mktemp)"
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
if ! $fetched; then
    if q git ls-remote --exit-code "$REMOTE" HEAD; then stop "$REMOTE has no '$BRANCH' branch"; fi
    stop "cannot reach GitHub ($REMOTE/$BRANCH)"
fi

# ── the version: ONE python helper reads, bumps and writes every kind of file ─
#   vtool read  KIND            < text  → version        (exit 1: unreadable)
#   vtool next  VERSION BUMP            → next version   (exit 1: not a version)
#   vtool write KIND NEW        < text  → text           (exit 3: no version field)
#   KIND: json (the TOP-LEVEL "version") | pyproject ([project] or [tool.poetry]) | plain | changelog
vtool() {
    python3 -c '
import datetime, json, re, sys
cmd, kind = sys.argv[1], sys.argv[2]

def pyproject_span(text):
    """(start, end) of the [project] or [tool.poetry] table body."""
    for table in ("project", "tool.poetry"):
        m = re.search(r"(?m)^\[" + re.escape(table) + r"\]\s*$", text)
        if m:
            nxt = re.search(r"(?m)^\[", text[m.end():])
            return m.end(), m.end() + (nxt.start() if nxt else len(text) - m.end())
    return None

VER = re.compile(r"(?m)^(version\s*=\s*\")([^\"]*)(\")")

if cmd == "read":
    text = sys.stdin.read()
    if kind == "json":
        print(json.loads(text)["version"])
    elif kind == "pyproject":
        a, b = pyproject_span(text)
        print(VER.search(text, a, b)[2])
    else:
        print(text.strip())
elif cmd == "next":
    cur, bump = sys.argv[2], sys.argv[3]
    m = re.match(r"^(v?)(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$", cur)
    if not m:
        sys.exit(1)
    v, a, b, c, pre = m[1], int(m[2]), int(m[3]), int(m[4]), m[5]
    if bump == "major":
        new = "%d.0.0" % (a if pre and b == 0 and c == 0 else a + 1)
    elif bump == "minor":
        new = "%d.%d.0" % (a, b if pre and c == 0 else b + 1)
    elif pre:
        new = "%d.%d.%d-%s" % (a, b, c, re.sub(r"\d+$", lambda n: str(int(n[0]) + 1), pre)
                               if re.search(r"\d+$", pre) else pre + ".1")
    else:
        new = "%d.%d.%d" % (a, b, c + 1)
    print(v + new)
elif cmd == "write":
    new = sys.argv[3]
    raw = sys.stdin.buffer.read().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = raw
    ok = False
    if kind == "json":
        # the TOP-LEVEL key: try each "version" string in turn, keep the one json agrees with
        for m in re.finditer(r"(\"version\"\s*:\s*\")([^\"]*)(\")", text):
            cand = text[:m.start(2)] + new + text[m.end(2):]
            try:
                if json.loads(cand).get("version") == new:
                    text, ok = cand, True
                    break
            except ValueError:
                pass
    elif kind == "pyproject":
        span = pyproject_span(text)
        if span:
            m = VER.search(text, *span)
            if m:
                text, ok = text[:m.start(2)] + new + text[m.end(2):], True
    elif kind == "plain":
        text, ok = new + nl, True
    elif kind == "changelog":
        m = re.search(r"(?m)^## \[?Unreleased\]?[ \t]*\r?$", text)
        if m:
            eol = text.find("\n", m.end())
            at = len(text) if eol == -1 else eol + 1
            head = "%s## %s - %s%s" % (nl, new, datetime.date.today().isoformat(), nl)
            text, ok = text[:at] + head + text[at:], True
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.exit(0 if ok else 3)
' "$@"
}
kind_of() {
    case "$1" in *.json) echo json ;; *pyproject.toml) echo pyproject ;; *) echo plain ;; esac
}
read_version() {   # tree → current version (a missing PLAIN version file starts at 0.0.0)
    local kind; kind="$(kind_of "$VERSION_FILE")"
    if ! git cat-file -e "$1:$VERSION_FILE" 2>/dev/null; then
        [[ "$kind" == plain ]] && { echo "0.0.0"; return 0; }
        return 1
    fi
    git cat-file -p "$1:$VERSION_FILE" | vtool read "$kind" 2>>"$LOG"
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
    if git merge-base --is-ancestor "$BASE" "$LOCAL_HEAD"; then   # only ahead: build on it
        PARENTS=(-p "$LOCAL_HEAD")
        BASE_TREE="$(git rev-parse "$LOCAL_HEAD^{tree}")"
        return 0
    fi
    local merged
    if merged="$(git merge-tree --write-tree "$BASE" "$LOCAL_HEAD" 2>/dev/null)"; then
        BASE_TREE="$merged"
        PARENTS=(-p "$BASE" -p "$LOCAL_HEAD")
    else
        finding "ERROR" "Git" "Local commits conflict with $REMOTE/$BRANCH — released $BRANCH without ${LOCAL_HEAD:0:9}" "./ship.sh (the sync sorts the conflict)"
        LOCAL_HEAD=""
    fi
}
refresh_remote_tags() {
    local t
    if t="$(git ls-remote --tags "$REMOTE" 2>>"$LOG")"; then REMOTE_TAGS="$t"; fi
}
tag_taken() {
    git rev-parse -q --verify "refs/tags/$1" >/dev/null && return 0
    grep -q "refs/tags/$1\$" <<< "$REMOTE_TAGS"
}
pick_version() {   # sets CURRENT, NEW, TAG, MSG from BASE_TREE
    CURRENT="$(read_version "$BASE_TREE")" || stop "cannot read the version in $VERSION_FILE"
    NEW="$(vtool next "$CURRENT" "$BUMP")" || stop "cannot bump the version '$CURRENT' in $VERSION_FILE"
    while tag_taken "${TAG_PREFIX}${NEW#v}"; do NEW="$(vtool next "$NEW" patch)"; done
    TAG="${TAG_PREFIX}${NEW#v}"
    MSG="release: ${TAG}${NOTE:+ - $NOTE}"
}
put_blob() {   # idx path kind label → bumped blob into the index, keeping its file mode
    local idx="$1" f="$2" kind="$3" mode blob
    mode="$(git ls-tree "$BASE_TREE" -- "$f" | awk '{print $1}')"
    if [[ "$mode" == 120000 ]]; then
        finding "WARNING" "Version" "$f is a symlink — its version was not bumped"; return 0
    fi
    if [[ -z "$mode" ]]; then
        if [[ "$f" == "$VERSION_FILE" && "$kind" == plain ]]; then
            mode=100644; blob="$(printf '%s\n' "$NEW" | git hash-object -w --stdin)"
        else
            finding "WARNING" "Version" "$f is not in the repository — its version was not bumped"; return 0
        fi
    elif ! blob="$(git cat-file -p "$BASE_TREE:$f" | vtool write "$kind" "$NEW" 2>>"$LOG" | git hash-object -w --stdin)"; then
        if [[ "$kind" == changelog ]]; then
            finding "WARNING" "Version" "$f has no '## Unreleased' heading — no release heading was added"
        else
            finding "WARNING" "Version" "$f has no version field to bump — left unchanged"
        fi
        return 0
    fi
    GIT_INDEX_FILE="$idx" git update-index --add --cacheinfo "$mode,$blob,$f"
}
build_commit() {   # sets RELEASE_SHA from BASE_TREE, NEW, MSG
    local idx f kind tree
    idx="$(mktemp)"; rm -f "$idx"
    GIT_INDEX_FILE="$idx" git read-tree "$BASE_TREE" || return 1
    put_blob "$idx" "$VERSION_FILE" "$(kind_of "$VERSION_FILE")" || return 1
    for f in ${EXTRA_VERSION_FILES[@]+"${EXTRA_VERSION_FILES[@]}"}; do
        kind="$(kind_of "$f")"
        if [[ "$kind" == plain ]]; then
            finding "WARNING" "Version" "$f is neither JSON nor a pyproject.toml — its version was not bumped"
            continue
        fi
        put_blob "$idx" "$f" "$kind" || return 1
    done
    if [[ -n "$CHANGELOG" ]]; then
        if git cat-file -e "$BASE_TREE:$CHANGELOG" 2>/dev/null; then
            put_blob "$idx" "$CHANGELOG" changelog || return 1
        else
            finding "WARNING" "Version" "$CHANGELOG is not in the repository — no release heading was added"
        fi
    fi
    tree="$(GIT_INDEX_FILE="$idx" git write-tree)" || return 1
    rm -f "$idx"
    RELEASE_SHA="$(git commit-tree "$tree" "${PARENTS[@]}" -m "$MSG" 2>>"$LOG")"
}

base_tree
REMOTE_TAGS=""
refresh_remote_tags
if $DRY; then
    pick_version
    echo "release.sh: DRY RUN — $REMOTE/$BRANCH is at $CURRENT; would release $TAG. Nothing changed."
    print_findings
    exit 0
fi

RACES=0; BLIPS=0; PUSHED=false
while (( RACES < 5 && BLIPS < 10 )); do
    pick_version
    build_commit || stop "could not assemble the release commit (see the log)"
    [[ -n "${RELEASE_TEST_BEFORE_PUSH:-}" ]] && { bash -c "$RELEASE_TEST_BEFORE_PUSH" >/dev/null 2>&1 || true; }
    if q git push --no-verify "$REMOTE" "$RELEASE_SHA:refs/heads/$BRANCH"; then PUSHED=true; break; fi
    seen="$(git rev-parse "$REMOTE/$BRANCH")"
    if q git fetch "$REMOTE" "$BRANCH" && [[ "$(git rev-parse "$REMOTE/$BRANCH")" != "$seen" ]]; then
        RACES=$((RACES + 1))
    else
        BLIPS=$((BLIPS + 1)); sleep $((BLIPS * 3))
    fi
    refresh_remote_tags
    base_tree
done
$PUSHED || stop "the push to GitHub failed ($RACES lost races, $BLIPS other failures — see the log)"

# ── from here the release is out; nothing below can fail it ──────────────────
if ! q git tag "$TAG" "$RELEASE_SHA" || ! q git push --no-verify "$REMOTE" "refs/tags/$TAG"; then
    finding "ERROR" "Git" "Tag $TAG did not reach GitHub" "git push $REMOTE $TAG"
fi
if [[ "$(git symbolic-ref -q --short HEAD)" == "$BRANCH" ]]; then
    q git -c core.hooksPath=/dev/null merge --ff-only "$REMOTE/$BRANCH" \
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
