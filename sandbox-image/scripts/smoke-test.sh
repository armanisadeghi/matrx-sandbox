#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# smoke-test.sh — Interactive smoke test for matrx_tools inside a sandbox
#
# Run from anywhere in the container:
#   bash /opt/sandbox/scripts/smoke-test.sh
#
# Or if you're already in the right place:
#   bash smoke-test.sh
#
# What it does:
#   1. Verifies the runtime environment (user, paths, binaries)
#   2. Dispatches every tool through the real Python dispatcher
#   3. Reports PASS/FAIL for each tool
#   4. Leaves behind a /tmp/smoke-test/ workspace you can inspect
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

PASS=0
FAIL=0
SKIP=0
WORKSPACE="/tmp/smoke-test"

pass()  { ((PASS++)); echo -e "  ${GREEN}✓ PASS${NC}  $1"; }
fail()  { ((FAIL++)); echo -e "  ${RED}✗ FAIL${NC}  $1: $2"; }
skip()  { ((SKIP++)); echo -e "  ${YELLOW}○ SKIP${NC}  $1: $2"; }
header(){ echo -e "\n${CYAN}── $1 ──${NC}"; }

# ─── Setup workspace ─────────────────────────────────────────────────────────
rm -rf "$WORKSPACE"
mkdir -p "$WORKSPACE"

# ─── 1. Environment checks ──────────────────────────────────────────────────
header "Environment"

echo -e "  User:       $(whoami) (uid=$(id -u))"
echo -e "  Home:       $HOME"
echo -e "  CWD:        $(pwd)"
echo -e "  HOT_PATH:   ${HOT_PATH:-<not set>}"
echo -e "  Python:     $(python3 --version 2>&1)"

# Check key binaries
for bin in rg fdfind git node pdftotext; do
    if command -v "$bin" &>/dev/null; then
        pass "$bin is available ($(command -v $bin))"
    else
        # fdfind might be 'fd' on some systems
        if [ "$bin" = "fdfind" ] && command -v fd &>/dev/null; then
            pass "fd is available ($(command -v fd))"
        else
            skip "$bin" "not found (may be expected)"
        fi
    fi
done

# Check Python package import
if python3 -c "from matrx_tools import TOOL_DEFINITIONS, dispatch, ToolSession; print(f'{len(TOOL_DEFINITIONS)} tools loaded')" 2>&1; then
    pass "matrx_tools package imports"
else
    fail "matrx_tools package import" "import failed"
    echo -e "\n${RED}Cannot continue without matrx_tools. Aborting.${NC}"
    exit 1
fi

# ─── 2. Dispatch every tool through Python ───────────────────────────────────
header "Tool dispatch tests"

# We write a Python script that exercises every tool through the real dispatcher
cat > "$WORKSPACE/run_tools.py" << 'PYTHON_SCRIPT'
"""Smoke test — dispatches every tool and reports results."""
import asyncio
import json
import os
import sys

# Make sure we can import
sys.path.insert(0, "/opt/sandbox/sdk")

from matrx_tools import TOOL_DEFINITIONS, dispatch, ToolSession
from matrx_tools.types import ToolResultType

WORKSPACE = "/tmp/smoke-test"
results = []

async def run_test(session, name, inputs, expect_error=False):
    """Run a single tool and record the result."""
    try:
        result = await dispatch(name, inputs, session)
        is_error = result.type == ToolResultType.ERROR
        if expect_error:
            ok = is_error
        else:
            ok = not is_error
        results.append({
            "tool": name,
            "ok": ok,
            "output": result.output[:200],
            "has_image": result.image is not None,
            "expected_error": expect_error,
        })
    except Exception as e:
        results.append({
            "tool": name,
            "ok": False,
            "output": f"EXCEPTION: {type(e).__name__}: {e}",
            "has_image": False,
            "expected_error": expect_error,
        })

async def main():
    session = ToolSession(working_dir=WORKSPACE)

    # ── Prepare test fixtures ─────────────────────────────────────────────
    os.makedirs(f"{WORKSPACE}/project/src", exist_ok=True)
    with open(f"{WORKSPACE}/hello.txt", "w") as f:
        f.write("Hello, sandbox!\nLine two.\nLine three.\n")
    with open(f"{WORKSPACE}/project/src/main.py", "w") as f:
        f.write('def greet(name):\n    return f"Hello, {name}!"\n\nif __name__ == "__main__":\n    print(greet("world"))\n')
    with open(f"{WORKSPACE}/test.ipynb", "w") as f:
        json.dump({
            "cells": [
                {"cell_type": "code", "id": "cell_0", "metadata": {}, "source": ["print('hi')"], "execution_count": None, "outputs": []},
                {"cell_type": "markdown", "id": "cell_1", "metadata": {}, "source": ["# Title"]},
            ],
            "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
            "nbformat": 4,
            "nbformat_minor": 5,
        }, f)

    # ── Read ──────────────────────────────────────────────────────────────
    await run_test(session, "Read", {"file_path": f"{WORKSPACE}/hello.txt"})

    # ── Read (non-existent — expect error) ────────────────────────────────
    await run_test(session, "Read", {"file_path": f"{WORKSPACE}/nope.txt"}, expect_error=True)

    # ── Write (new file) ──────────────────────────────────────────────────
    await run_test(session, "Write", {
        "file_path": f"{WORKSPACE}/written.txt",
        "content": "Written by smoke test\n",
    })

    # ── Write (existing file without read — expect error) ─────────────────
    await run_test(session, "Write", {
        "file_path": f"{WORKSPACE}/hello.txt",
        "content": "overwrite attempt",
    }, expect_error=True)

    # ── Edit ──────────────────────────────────────────────────────────────
    # Read it first (already read above), then edit
    await run_test(session, "Edit", {
        "file_path": f"{WORKSPACE}/hello.txt",
        "old_string": "Line two.",
        "new_string": "Line two (edited).",
    })

    # ── MultiEdit ─────────────────────────────────────────────────────────
    await run_test(session, "Read", {"file_path": f"{WORKSPACE}/project/src/main.py"})
    await run_test(session, "MultiEdit", {
        "file_path": f"{WORKSPACE}/project/src/main.py",
        "edits": [
            {"old_string": "Hello", "new_string": "Hi"},
            {"old_string": "world", "new_string": "sandbox"},
        ],
    })

    # ── Glob ──────────────────────────────────────────────────────────────
    await run_test(session, "Glob", {"pattern": "*.txt", "path": WORKSPACE})

    # ── Grep ──────────────────────────────────────────────────────────────
    await run_test(session, "Grep", {
        "pattern": "sandbox",
        "path": WORKSPACE,
        "output_mode": "content",
    })

    # ── NotebookEdit ──────────────────────────────────────────────────────
    await run_test(session, "Read", {"file_path": f"{WORKSPACE}/test.ipynb"})
    await run_test(session, "NotebookEdit", {
        "notebook_path": f"{WORKSPACE}/test.ipynb",
        "cell_id": "cell_0",
        "new_source": "print('updated by smoke test')",
        "edit_mode": "replace",
    })

    # ── Bash (foreground) ─────────────────────────────────────────────────
    await run_test(session, "Bash", {"command": "echo 'Hello from Bash' && pwd"})

    # ── Bash (cwd tracking) ───────────────────────────────────────────────
    await run_test(session, "Bash", {"command": "cd /tmp && pwd"})
    cwd_after = session.cwd
    results.append({
        "tool": "Bash(cwd-tracking)",
        "ok": cwd_after == "/tmp",
        "output": f"session.cwd after 'cd /tmp': {cwd_after}",
        "has_image": False,
        "expected_error": False,
    })
    # Reset cwd
    session.cwd = WORKSPACE

    # ── Bash (background) ─────────────────────────────────────────────────
    await run_test(session, "Bash", {
        "command": "sleep 0.2 && echo 'bg done'",
        "run_in_background": True,
    })

    # Give it a moment, then check output
    await asyncio.sleep(0.5)
    shell_ids = list(session.background_shells.keys())
    if shell_ids:
        await run_test(session, "BashOutput", {"bash_id": shell_ids[0]})
    else:
        results.append({"tool": "BashOutput", "ok": False, "output": "No background shells", "has_image": False, "expected_error": False})

    # ── TaskStop (on the already-completed shell — should report completed)
    if shell_ids:
        await run_test(session, "TaskStop", {"task_id": shell_ids[0]})

    # ── WebFetch (placeholder) ────────────────────────────────────────────
    await run_test(session, "WebFetch", {"url": "https://example.com", "prompt": "test"})

    # ── WebSearch (placeholder) ───────────────────────────────────────────
    await run_test(session, "WebSearch", {"query": "test query"})

    # ── TodoWrite ─────────────────────────────────────────────────────────
    await run_test(session, "TodoWrite", {"todos": [
        {"content": "Test task", "status": "in_progress", "activeForm": "Testing task"},
    ]})

    # ── BrowserNavigate ───────────────────────────────────────────────────
    try:
        await run_test(session, "BrowserNavigate", {"url": "https://example.com"})
        browser_ok = True
    except Exception:
        browser_ok = False
        results.append({"tool": "BrowserNavigate", "ok": False, "output": "Playwright not available", "has_image": False, "expected_error": False})

    if browser_ok and session.browser.is_running:
        # ── BrowserSnapshot ───────────────────────────────────────────────
        await run_test(session, "BrowserSnapshot", {})

        # ── BrowserScreenshot ─────────────────────────────────────────────
        await run_test(session, "BrowserScreenshot", {})

        # ── BrowserClick ──────────────────────────────────────────────────
        await run_test(session, "BrowserClick", {"text": "More information"})

        # ── BrowserType (no focused input — may error, that's ok) ─────────
        await run_test(session, "BrowserEvaluate", {"javascript": "document.title"})

        # ── BrowserPressKey ───────────────────────────────────────────────
        await run_test(session, "BrowserPressKey", {"key": "Escape"})

        # ── BrowserScroll ─────────────────────────────────────────────────
        await run_test(session, "BrowserScroll", {"direction": "down", "amount": 2})

        # ── BrowserWaitFor ────────────────────────────────────────────────
        await run_test(session, "BrowserBack", {})
        await run_test(session, "BrowserWaitFor", {"text": "Example Domain", "timeout": 5000})

        # ── BrowserTabs ───────────────────────────────────────────────────
        await run_test(session, "BrowserTabs", {"action": "list"})

        # ── BrowserConsole ────────────────────────────────────────────────
        await run_test(session, "BrowserConsole", {})

        # ── BrowserClose ──────────────────────────────────────────────────
        await run_test(session, "BrowserClose", {})
    else:
        for name in ["BrowserSnapshot", "BrowserScreenshot", "BrowserClick",
                      "BrowserEvaluate", "BrowserPressKey", "BrowserScroll",
                      "BrowserBack", "BrowserWaitFor", "BrowserTabs",
                      "BrowserConsole", "BrowserClose"]:
            results.append({"tool": name, "ok": False, "output": "Browser not available — skipped", "has_image": False, "expected_error": False})

    # ── Cleanup ───────────────────────────────────────────────────────────
    await session.cleanup()

    # ── Print results ─────────────────────────────────────────────────────
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        extra = ""
        if r["has_image"]:
            extra = " [+image]"
        if r["expected_error"]:
            extra += " (expected error)"
        print(f"{status}|{r['tool']}|{r['output'][:150]}{extra}")

asyncio.run(main())
PYTHON_SCRIPT

# Run the Python test script
echo ""
python3 "$WORKSPACE/run_tools.py" 2>/dev/null | while IFS='|' read -r status tool output; do
    if [ "$status" = "PASS" ]; then
        pass "$tool — $output"
    else
        fail "$tool" "$output"
    fi
done

# ─── 3. File permission checks ──────────────────────────────────────────────
header "Permission checks"

# Can we write to home?
if touch "$HOME/.smoke_test_marker" 2>/dev/null; then
    rm -f "$HOME/.smoke_test_marker"
    pass "Write to \$HOME ($HOME)"
else
    fail "Write to \$HOME" "permission denied"
fi

# Can we write to /tmp?
if touch /tmp/.smoke_test_marker 2>/dev/null; then
    rm -f /tmp/.smoke_test_marker
    pass "Write to /tmp"
else
    fail "Write to /tmp" "permission denied"
fi

# Can we run git?
if git init "$WORKSPACE/git-test" &>/dev/null; then
    pass "git init works"
    rm -rf "$WORKSPACE/git-test"
else
    fail "git init" "failed"
fi

# Can we install Python packages?
if pip install --quiet --target="$WORKSPACE/pip-test" six 2>/dev/null; then
    pass "pip install works (installed 'six' to temp dir)"
    rm -rf "$WORKSPACE/pip-test"
else
    skip "pip install" "may require network"
fi

# Can we run node?
if node -e "console.log('node ok')" &>/dev/null; then
    pass "node execution works"
else
    skip "node" "not available"
fi

# ─── 4. Summary ──────────────────────────────────────────────────────────────
header "Summary"
echo -e "  ${GREEN}Passed: $PASS${NC}"
echo -e "  ${RED}Failed: $FAIL${NC}"
echo -e "  ${YELLOW}Skipped: $SKIP${NC}"
echo -e "  Workspace: $WORKSPACE (preserved for inspection)"
echo ""

if [ "$FAIL" -eq 0 ]; then
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}$FAIL test(s) failed.${NC}"
    exit 1
fi
