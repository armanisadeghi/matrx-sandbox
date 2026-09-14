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
# The node half needs a registry; set MATRX_TOOLCHAIN_SKIP_NODE_INSTALL=1 to
# check scaffolding only (the scaffold itself is still verified).
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "  PASS  $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL  $1: $2"; }

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
