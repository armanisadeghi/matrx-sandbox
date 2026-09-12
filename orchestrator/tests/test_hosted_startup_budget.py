"""Execute the real deployment validation/wait blocks without Docker or traffic."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deploy-hosted.sh"


@pytest.mark.parametrize("value", ["0", "29", "1801", "-1", "abc", "0300"])
def test_invalid_budget_refused_before_deployment(tmp_path, value):
    prefix = SCRIPT.read_text().split('SCRIPT_DIR=')[0]
    result = subprocess.run(
        ["bash", "-c", prefix], capture_output=True, text=True,
        env={**os.environ, "ORCH_STARTUP_TIMEOUT_SECONDS": value,
             "DEPLOY_FAILURE_FILE": str(tmp_path / "failure.json")},
    )
    assert result.returncode != 0
    assert "ORCH_STARTUP_TIMEOUT_SECONDS" in result.stderr


def test_elapsed_deadline_clamps_last_request_and_reports_failure(tmp_path):
    source = SCRIPT.read_text()
    block = source[source.index("verified=0\n"):source.index("# Refresh the out-of-checkout")]
    result = subprocess.run(["bash", "-c", '''
ORCH_STARTUP_TIMEOUT_SECONDS=31
ORCH_API_KEY=synthetic
ORCH_HEALTH_URL=https://unused.invalid/health
NEW_SHA=synthetic
unset SECONDS
SECONDS=0
curl() { printf '%s\n' "$*" >> "$CALLS"; return 1; }
sleep() { SECONDS=$((SECONDS + $1)); }
log() { echo "$*"; }
fail_release() { echo "$*" >&2; exit 7; }
''' + block], capture_output=True, text=True,
        env={**os.environ, "CALLS": str(tmp_path / "calls")})
    assert result.returncode == 7
    assert "after 31s (startup budget 31s)" in result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert len(calls) == 16
    assert "--max-time 1 " in calls[-1]


def test_deadline_tick_before_remaining_never_calls_curl_with_zero(tmp_path):
    source = SCRIPT.read_text()
    block = source[source.index("verified=0\n"):source.index("# Refresh the out-of-checkout")]
    result = subprocess.run(["bash", "-c", '''
ORCH_STARTUP_TIMEOUT_SECONDS=31
ORCH_API_KEY=synthetic
ORCH_HEALTH_URL=https://unused.invalid/health
NEW_SHA=synthetic
unset SECONDS
SECONDS=0
curl() { echo CURL_MUST_NOT_RUN >> "$CALLS"; return 1; }
sleep() { :; }
log() { :; }
fail_release() { echo "$*" >&2; exit 7; }
trap 'if [[ "$BASH_COMMAND" == "remaining="* ]]; then SECONDS=$ORCH_WAIT_DEADLINE; fi' DEBUG
''' + block], capture_output=True, text=True, timeout=5,
        env={**os.environ, "CALLS": str(tmp_path / "calls")})
    assert result.returncode == 7
    assert not (tmp_path / "calls").exists()
    assert "after 31s" in result.stderr
