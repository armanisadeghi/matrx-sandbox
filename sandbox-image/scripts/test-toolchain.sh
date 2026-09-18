#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# test-toolchain.sh — forcing-function check for the agent project toolchain.
#
# Green ONLY when a real agent can actually set up a project in this image in
# one command. It runs the real binaries and the real `mtx new` scaffold, then
# really runs the generated test suite. No mocks, no stubs.
#
# Run inside a container as the agent user:
#   docker run --rm -u agent matrx-sandbox:slim bash /opt/sandbox/scripts/test-toolchain.sh
#
# Exit 0 = pass, 1 = fail (with the failing check named).
#
# Why it exists: on 2026-09-14 the sandbox agent burned 3-4 failed tool calls
# per Python setup because `uv` was absent from the bare/slim/core images and
# the model improvised flags. This is the guard that keeps it present.
#
# Extended 2026-09-18 with the VERSION MATRIX: the five-defect toolchain
# contract (node >= 22, agent-writable npm global prefix, install scripts
# that actually run, a RELEASE python3 >= 3.12, `browse` preinstalled). See
# the block comment on that section and ADDING_UTILITIES.md.
#
# The node half needs a registry; set MATRX_TOOLCHAIN_SKIP_NODE_INSTALL=1 to
# check scaffolding only (the scaffold itself is still verified).
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "  PASS  $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL  $1: $2"; }

# ─────────────────────────────────────────────────────────────────────────────
# THE VERSION MATRIX — the toolchain contract, proved on the box you are in.
#
# Added 2026-09-18 after a field report from an agent working in a real EC2
# "development" box found five defects that every image variant shared:
#
#   P1-1  Node 20 → Stagehand v4 died: `ReferenceError: WebSocket is not
#         defined` (it needs Node >= 22).
#   P1-2  `npm i -g <pkg>` as the agent user → EACCES on root-owned
#         /usr/lib/node_modules.
#   P1-3  npm 11's `allow-scripts` allowlist SKIPS a dependency's postinstall
#         with only a warning unless a policy is set — half the native JS
#         ecosystem installs broken and says almost nothing.
#   P2-1  `python3` was 3.11.0rc1 — Ubuntu 22.04's python3.11 package is a 2022
#         RELEASE CANDIDATE. Agents ran pre-release CPython for months.
#   P2-2  `browse` was not installed; agents typed it and got `command not
#         found`.
#
# Every check below runs the real binary and asserts the real outcome. They are
# red on any image built before 2026-09-18 and green on the current one; that
# red→green is the evidence, not this comment.
# ─────────────────────────────────────────────────────────────────────────────
echo "== version matrix (the toolchain contract) =="

# ── node >= 22 ──────────────────────────────────────────────────────────────
if node_v="$(node --version 2>&1)"; then
    node_major="${node_v#v}"; node_major="${node_major%%.*}"
    if [ "${node_major:-0}" -ge 22 ] 2>/dev/null; then
        pass "node $node_v (>= 22)"
    else
        fail "node >= 22" "this box has $node_v — Stagehand v4 and friends need Node 22+"
    fi
else
    fail "node --version" "$node_v"
fi

# ── npm global install works AS THIS USER (no sudo, no EACCES) ──────────────
if [ "${MATRX_TOOLCHAIN_SKIP_NODE_INSTALL:-0}" = "1" ]; then
    echo "  SKIP  npm install -g as $(id -un) (MATRX_TOOLCHAIN_SKIP_NODE_INSTALL=1)"
else
    if out="$(npm install -g cowsay 2>&1)"; then
        if cowsay -t "npm -g works" >/dev/null 2>&1; then
            pass "npm install -g as $(id -un) -> $(command -v cowsay)"
        else
            fail "npm install -g as $(id -un)" "installed but the binary is not runnable from PATH"
        fi
        npm uninstall -g cowsay >/dev/null 2>&1
    else
        fail "npm install -g as $(id -un)" "$(echo "$out" | grep -iE 'EACCES|error' | head -3)"
    fi
fi

# ── the npm global prefix is image-owned and this user can write it ─────────
NPM_PREFIX="$(npm config get prefix 2>/dev/null)"
case "$NPM_PREFIX" in
    /home/*) fail "npm global prefix" "$NPM_PREFIX is inside the home — a home restore or image swap wipes or staleness-freezes it; it must be image-owned (/opt/npm-global)" ;;
    "") fail "npm global prefix" "npm reported no prefix" ;;
    *)
        if [ -w "$NPM_PREFIX/lib" ]; then
            pass "npm global prefix $NPM_PREFIX is writable by $(id -un)"
        else
            fail "npm global prefix" "$NPM_PREFIX/lib is not writable by $(id -un) — this is the EACCES defect"
        fi
        ;;
esac

# ── a dependency's postinstall actually RUNS ────────────────────────────────
# npm 11 warns "not yet covered by allowScripts" and skips it when no policy is
# set. Both halves are asserted: the sentinel file AND the absence of the warning
# (a warning means the next package with a real postinstall gets skipped).
SCRIPTS_WORK="$(mktemp -d)"
mkdir -p "$SCRIPTS_WORK/child" "$SCRIPTS_WORK/app"
SENTINEL="$SCRIPTS_WORK/POSTINSTALL_RAN"
cat > "$SCRIPTS_WORK/child/package.json" <<JSON
{
  "name": "matrx-postinstall-probe",
  "version": "1.0.0",
  "scripts": { "postinstall": "node -e \"require('fs').writeFileSync(process.env.MATRX_SENTINEL,'ran')\"" }
}
JSON
cat > "$SCRIPTS_WORK/app/package.json" <<JSON
{ "name": "matrx-postinstall-app", "version": "1.0.0",
  "dependencies": { "matrx-postinstall-probe": "file:../child" } }
JSON
if [ "${MATRX_TOOLCHAIN_SKIP_NODE_INSTALL:-0}" = "1" ]; then
    echo "  SKIP  dependency postinstall runs (MATRX_TOOLCHAIN_SKIP_NODE_INSTALL=1)"
else
    out="$(cd "$SCRIPTS_WORK/app" && MATRX_SENTINEL="$SENTINEL" npm install 2>&1)"
    if [ -f "$SENTINEL" ] && ! echo "$out" | grep -q "allowScripts"; then
        pass "a dependency's postinstall runs (install-script policy is set)"
    elif echo "$out" | grep -q "allowScripts"; then
        fail "dependency postinstall" "npm skipped it: $(echo "$out" | grep -m1 -A1 allowScripts | tr '\n' ' ')"
    else
        fail "dependency postinstall" "the postinstall never wrote $SENTINEL"
    fi
fi
rm -rf "$SCRIPTS_WORK"

# ── python3 is a FINAL release >= 3.12 ──────────────────────────────────────
# Asserted through the interpreter itself, not a string match: Ubuntu 22.04's
# python3.11 reports "3.11.0rc1", and releaselevel is the field that catches it.
if out="$(python3 -c 'import sys; v=sys.version_info; assert v[:2] >= (3,12) and v.releaselevel == "final", sys.version; print(sys.version.split()[0])' 2>&1)"; then
    pass "python3 is a release build -> $out"
else
    fail "python3 release >= 3.12" "$(python3 --version 2>&1) — $(echo "$out" | tail -1)"
fi

# ── the interpreter `mtx` runs on can import the SDK ────────────────────────
if out="$(/usr/bin/python3 -c 'import matrx_agent, matrx_tools, sys; print(sys.version.split()[0])' 2>&1)"; then
    pass "/usr/bin/python3 imports the SDK -> $out"
else
    fail "/usr/bin/python3 imports the SDK" "$(echo "$out" | tail -2)"
fi

# ── `browse` is preinstalled ────────────────────────────────────────────────
if out="$(browse --help 2>&1)"; then
    pass "browse --help -> $(echo "$out" | head -1)"
else
    fail "browse --help" "$(echo "$out" | tail -2)"
fi

echo "== mtx toolchain ensure (self-service upgrade, must be a safe no-op here) =="
if out="$(mtx toolchain ensure 2>&1)"; then
    pass "mtx toolchain ensure -> $(echo "$out" | tail -1)"
else
    fail "mtx toolchain ensure" "$(echo "$out" | tail -3)"
fi
if mtx toolchain check >/dev/null 2>&1; then
    pass "mtx toolchain check (every mandated binary present)"
else
    fail "mtx toolchain check" "$(mtx toolchain check 2>&1 | tail -3)"
fi

echo "== toolchain binaries =="
for bin in uv pnpm gh; do
    if out="$("$bin" --version 2>&1)"; then
        pass "$bin --version -> $(echo "$out" | head -1)"
    else
        fail "$bin --version" "$(echo "$out" | head -1)"
    fi
done

echo "== mtx new python =="
WORK="$(mktemp -d)"
export MATRX_PROJECTS_ROOT="$WORK/projects"

if out="$(mtx new python demo 2>&1)"; then
    pass "mtx new python demo"
else
    fail "mtx new python demo" "$(echo "$out" | tail -3)"
fi

if [ -f "$MATRX_PROJECTS_ROOT/demo/pyproject.toml" ] \
   && [ -f "$MATRX_PROJECTS_ROOT/demo/test_demo.py" ]; then
    pass "flat project laid down (pyproject.toml + test_demo.py at the root)"
else
    fail "flat project layout" "expected pyproject.toml and test_demo.py in $MATRX_PROJECTS_ROOT/demo"
fi

if [ -d "$MATRX_PROJECTS_ROOT/demo" ]; then
    if out="$(cd "$MATRX_PROJECTS_ROOT/demo" && uv run pytest -q 2>&1)"; then
        pass "uv run pytest (generated suite passes)"
    else
        fail "uv run pytest" "$(echo "$out" | tail -5)"
    fi
fi

echo "== mtx new node =="
if out="$(mtx new node webdemo 2>&1)"; then
    pass "mtx new node webdemo"
else
    fail "mtx new node webdemo" "$(echo "$out" | tail -3)"
fi

if [ -f "$MATRX_PROJECTS_ROOT/webdemo/package.json" ] \
   && [ -f "$MATRX_PROJECTS_ROOT/webdemo/webdemo.test.js" ]; then
    pass "flat node project laid down (package.json + webdemo.test.js at the root)"
else
    fail "flat node project layout" "expected package.json and webdemo.test.js in $MATRX_PROJECTS_ROOT/webdemo"
fi

if [ "${MATRX_TOOLCHAIN_SKIP_NODE_INSTALL:-0}" = "1" ]; then
    echo "  SKIP  pnpm install && pnpm test (MATRX_TOOLCHAIN_SKIP_NODE_INSTALL=1)"
elif [ -d "$MATRX_PROJECTS_ROOT/webdemo" ]; then
    if out="$(cd "$MATRX_PROJECTS_ROOT/webdemo" && pnpm install --silent && pnpm test 2>&1)"; then
        pass "pnpm install && pnpm test (generated suite passes)"
    else
        fail "pnpm install && pnpm test" "$(echo "$out" | tail -5)"
    fi
fi

rm -rf "$WORK"

echo
echo "toolchain: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
