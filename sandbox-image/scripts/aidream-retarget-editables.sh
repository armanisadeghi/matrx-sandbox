#!/usr/bin/env bash
# Retarget uv editable `.pth` files in a seeded aidream working copy.
#
# WHY THIS IS ITS OWN SCRIPT (2026-09-18, feedback 81cb4265): this ran inline in
# `entrypoint-aidream.sh`, where it counted LOOP ITERATIONS instead of successful
# rewrites. On a persistent volume holding an older, root-owned seed, `sed -i`
# failed 17 times with "couldn't open temporary file … Permission denied" and the
# step still logged `retargeted 17 editable .pth file(s)`. A boot step that
# reports success having written nothing is worse than one that fails: every
# `import matrx_ai` kept resolving to the immutable template and nobody looked.
# The work is unreachable from a test while it sits behind a 2.7 GB seed, so it
# lives here and says exactly what it did — the same move `aidream-downstream-
# entrypoint.sh` made for the tier decision.
#
# Usage: aidream-retarget-editables.sh <template-dir> <work-dir>
#
# Exit 0 = every editable `.pth` now points at <work-dir>.
# Exit 3 = at least one could not be rewritten; every failure is named on stderr.
# Exit 0 with "no editable .pth files" = nothing to do, said out loud.

set -uo pipefail

TEMPLATE_DIR="${1:-}"
WORK_DIR="${2:-}"

log() { echo "[retarget-editables] $*"; }
fail() { echo "[retarget-editables] $*" >&2; }

if [ -z "$TEMPLATE_DIR" ] || [ -z "$WORK_DIR" ]; then
    fail "usage: aidream-retarget-editables.sh <template-dir> <work-dir>"
    exit 2
fi

# The venv's python version is NOT hard-coded. It used to be `python3.13`, so a
# venv built on any other minor version took the "no site-packages" return and
# the boot logged a skip that read like a clean no-op.
shopt -s nullglob
site_dirs=("$WORK_DIR"/.venv/lib/python*/site-packages)
if [ ${#site_dirs[@]} -eq 0 ]; then
    log "no .venv site-packages under $WORK_DIR/.venv/lib/python*/ — nothing to retarget"
    exit 0
fi

pth_files=()
for site_pkgs in "${site_dirs[@]}"; do
    for pth in "$site_pkgs"/_editable_impl_*.pth; do
        [ -f "$pth" ] || continue
        pth_files+=("$pth")
    done
done

if [ ${#pth_files[@]} -eq 0 ]; then
    log "no editable .pth files under ${site_dirs[*]} — nothing to retarget"
    exit 0
fi

# An older seed left this tree root-owned and `a-w` (the image template is built
# read-only on purpose and `cp -a` preserves that). `sed -i` writes a temp file
# IN the directory, so it needs the directory writable by whoever runs this.
# Repair ownership here, as root, while we are still in the root boot phase —
# BEFORE `ensure-layout.sh`, which is the one chokepoint for THE HOME OWNERSHIP
# LAW and must stay the last word on /home/agent.
repair_ownership() {
    local target="$1"
    [ "$(id -u)" = "0" ] || return 1
    chown -R agent:agent "$target" 2>/dev/null || return 1
    chmod -R u+w "$target" 2>/dev/null || return 1
    return 0
}

# `sed -i` is deliberately NOT used: its temp file lands in the .pth's own
# directory under a name we cannot see, which is what made the original failure
# unreadable, and its flag spelling differs between GNU and BSD sed so the
# behaviour could not be exercised off a Linux box. Same permission semantics
# (the temp file is still created in that directory, so an unwritable seed still
# fails here), but we own the name and can clean it up.
rewrite_pth() {
    local pth="$1"
    local tmp="$pth.retarget.$$"
    if sed "s|$TEMPLATE_DIR|$WORK_DIR|g" "$pth" > "$tmp" 2>/dev/null \
        && cat "$tmp" > "$pth" 2>/dev/null; then
        rm -f "$tmp"
        return 0
    fi
    rm -f "$tmp" 2>/dev/null || true
    return 1
}

retargeted=0
skipped=0
failures=()

for pth in "${pth_files[@]}"; do
    if ! grep -q -- "$TEMPLATE_DIR" "$pth" 2>/dev/null; then
        skipped=$((skipped + 1))
        continue
    fi
    if ! rewrite_pth "$pth"; then
        # One repair attempt, announced, then retry. Never a silent chown of a
        # single path: the whole seeded tree is what the old seed got wrong.
        log "cannot rewrite $pth — repairing ownership of $WORK_DIR and retrying"
        if ! repair_ownership "$WORK_DIR" || ! rewrite_pth "$pth"; then
            failures+=("$pth")
            continue
        fi
    fi
    # Proof, not optimism: the file must no longer name the template.
    if grep -q -- "$TEMPLATE_DIR" "$pth" 2>/dev/null; then
        failures+=("$pth")
        continue
    fi
    retargeted=$((retargeted + 1))
done

total=${#pth_files[@]}
if [ ${#failures[@]} -gt 0 ]; then
    fail "retargeted $retargeted of $total editable .pth file(s); ${#failures[@]} still point at $TEMPLATE_DIR:"
    for path in "${failures[@]}"; do
        fail "  $path"
    done
    fail "REMEDY: every 'import' from $WORK_DIR will resolve to the immutable template instead of the user's checkout."
    fail "REMEDY: run 'mtx aidream reset --force' to re-seed the working copy, or chown -R agent:agent $WORK_DIR as root and re-run this script."
    exit 3
fi

log "retargeted $retargeted of $total editable .pth file(s) to $WORK_DIR ($skipped already correct)"
exit 0
