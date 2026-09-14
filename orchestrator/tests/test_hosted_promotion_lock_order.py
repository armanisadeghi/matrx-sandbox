"""The hosted promotion lock contract, executed as the real deploy-hosted.sh bash.

Two behaviours, both named by the 2026-09-14 stability sweep and both closed
here, are guarded by running the REAL functions out of the REAL script (and the
REAL `release_lock_holder.py`) with only `docker` and `log` stubbed:

1. **Bounded wait.** The shared `deployment` lease is also taken, for a few
   hundred milliseconds, by the 60-second liveness reconcile sweep. A single
   non-blocking attempt therefore threw away a fully built, fully verified
   candidate on roughly two poller ticks in five. `DEPLOY_LOCK_WAIT_SECONDS`
   (default 120, 0 = the old non-blocking behaviour) waits it out.

2. **Seize before pause.** The bootstrap path used to `docker pause` the live
   orchestrator and only *then* find out whether it could seize the locks, so a
   contended or invalid seize froze a healthy edge for nothing. The seize now
   happens first, against the running control, and a failure leaves it running.
   What the pause used to buy is bought instead by re-censusing the lock set
   once the control is frozen.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(os.environ.get("MATRX_DEPLOY_HOSTED_SCRIPT", ROOT / "scripts/deploy-hosted.sh"))


def _bash() -> str:
    """A bash that has `coproc` (4.0+). macOS ships 3.2 as /bin/bash."""
    for candidate in (os.environ.get("BASH"), "/opt/homebrew/bin/bash",
                      "/usr/local/bin/bash", shutil.which("bash"), "/bin/bash"):
        if not candidate or not Path(candidate).exists():
            continue
        out = subprocess.run([candidate, "-c", "echo ${BASH_VERSINFO[0]}"],
                             capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip().isdigit() and int(out.stdout.strip()) >= 4:
            return candidate
    pytest.skip("no bash >= 4 available (coproc is required by the lock holder)")


def _extract(name: str) -> str:
    source = SCRIPT.read_text()
    marker = f"\n{name}() {{"
    assert marker in source, f"{name} is missing from {SCRIPT}"
    body = source.split(marker, 1)[1].split("\n}\n", 1)[0]
    return f"{name}() {{{body}\n}}\n"


# ── 1. The bounded wait, against the real release_lock_holder.py ─────────────

LOCK_FUNCS = ("release_migration_lock_holder", "start_migration_lock_holder")


def _run_holder(journal: Path, wait: str) -> tuple[int, float, str]:
    harness = f'''
set -uo pipefail
REPO_DIR="{ROOT}"
HOSTED_MIGRATION_STATE_DIR="{journal}"
MIGRATION_LOCK_HOLDER_PID=""
MIGRATION_LOCK_HOLDER_INPUT_FD=""
MIGRATION_LOCK_HOLDER_OUTPUT_FD=""
'''
    script = (harness + "".join(_extract(f) for f in LOCK_FUNCS)
              + f'\nstatus=0\nstart_migration_lock_holder named {wait} deployment || status=$?\n'
                'release_migration_lock_holder || true\necho "RC=$status"\n')
    started = time.monotonic()
    result = subprocess.run([_bash(), "-c", script], capture_output=True, text=True)
    elapsed = time.monotonic() - started
    combined = result.stdout + result.stderr
    assert "RC=" in combined, combined
    return int(combined.strip().rsplit("RC=", 1)[1].split()[0]), elapsed, combined


@pytest.fixture()
def journal(tmp_path: Path) -> Path:
    directory = tmp_path / "hosted-migrations"
    directory.mkdir()
    lock = directory / "deployment.lock"
    lock.touch(mode=0o600)
    return directory


def test_uncontended_acquisition_is_immediate(journal):
    rc, elapsed, out = _run_holder(journal, "120")
    assert rc == 0, out
    assert elapsed < 10, "a free lock must not pay any part of the wait budget"


def test_zero_keeps_the_old_nonblocking_behaviour(journal):
    with (journal / "deployment.lock").open("r+b") as rival:
        fcntl.flock(rival, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, elapsed, out = _run_holder(journal, "0")
    assert rc == 75, f"0 must mean one non-blocking attempt:\n{out}"
    assert elapsed < 10, "the non-blocking path must not wait at all"


def test_bounded_wait_outlasts_a_short_lived_holder(journal):
    """The reconcile sweep holds this lease for a moment; a deploy can wait."""
    rival = (journal / "deployment.lock").open("r+b")
    fcntl.flock(rival, fcntl.LOCK_EX | fcntl.LOCK_NB)
    released = threading.Event()

    def _release():
        time.sleep(1.5)
        fcntl.flock(rival, fcntl.LOCK_UN)
        rival.close()
        released.set()

    releaser = threading.Thread(target=_release, daemon=True)
    releaser.start()
    try:
        rc, elapsed, out = _run_holder(journal, "30")
    finally:
        releaser.join(timeout=30)
    assert released.is_set()
    assert rc == 0, f"a bounded wait must ride out a short-lived holder:\n{out}"
    assert elapsed >= 1.3, f"it must actually have waited, not raced: {elapsed}s"


def test_bounded_wait_still_gives_up_and_defers(journal):
    with (journal / "deployment.lock").open("r+b") as rival:
        fcntl.flock(rival, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, elapsed, out = _run_holder(journal, "2")
    assert rc == 75, f"the wait is BOUNDED — a held lock must still defer:\n{out}"
    assert 1.5 <= elapsed < 25, f"it must wait the budget and no longer: {elapsed}s"


def test_the_wait_knob_is_validated_and_wired_to_the_safe_path():
    source = SCRIPT.read_text()
    assert 'DEPLOY_LOCK_WAIT_SECONDS="${DEPLOY_LOCK_WAIT_SECONDS:-120}"' in source
    assert "DEPLOY_LOCK_WAIT_SECONDS must be a whole number" in source
    assert 'start_migration_lock_holder named "$DEPLOY_LOCK_WAIT_SECONDS" deployment' in source


# ── 2. Seize before pause, on the bootstrap path ─────────────────────────────

BOOTSTRAP_FUNC = "bootstrap_barrier_seize"


def _run_bootstrap(tmp_path: Path, *, seize_status: int = 0,
                   census_drifts: bool = False) -> tuple[int, str, list[str]]:
    trace = tmp_path / "trace"
    harness = f'''
set -uo pipefail
TRACE="{trace}"
PREVIOUS_ORCH_CONTAINER_ID=orch-old
AUDIT_IMAGE=audit:image
BOOTSTRAP_OLD_PAUSED=0
ORCH_STOPPED=0
CENSUS_COUNTER="{tmp_path}/census-calls"
log() {{ printf '%s\\n' "$*"; }}
fail_release() {{ printf 'FAIL_RELEASE: %s\\n' "$*"; exit 9; }}
acquire_frozen_old_locks() {{ printf 'seize\\n' >> "$TRACE"; return {seize_status}; }}
audit_migration_release_state() {{ printf 'audit\\n' >> "$TRACE"; return 0; }}
lock_census() {{
  # a file, not a variable: the caller reads this through $( ), a subshell
  printf 'x' >> "$CENSUS_COUNTER"
  if [ "{1 if census_drifts else 0}" = 1 ] && [ "$(wc -c < "$CENSUS_COUNTER")" -gt 1 ]; then
    printf 'deployment.lock sbx-new.lock '
  else
    printf 'deployment.lock '
  fi
}}
docker() {{
  printf '%s\\n' "$1" >> "$TRACE"
  case "$*" in
    *State.Status*) printf 'paused\\n' ;;
    *State.Running*) printf 'false\\n' ;;
  esac
  return 0
}}
'''
    script = harness + _extract(BOOTSTRAP_FUNC) + '\nbootstrap_barrier_seize\necho "RC=$?"\n'
    result = subprocess.run([_bash(), "-c", script], capture_output=True, text=True)
    steps = trace.read_text().split() if trace.exists() else []
    return result.returncode, result.stdout + result.stderr, steps


def test_a_failed_seize_never_touches_the_live_orchestrator(tmp_path):
    rc, out, steps = _run_bootstrap(tmp_path, seize_status=1)
    assert rc == 9, out
    assert "pause" not in steps, (
        "THE RULING: a contended seize must leave the live orchestrator running — "
        f"instead the script did: {steps}"
    )
    assert "kill" not in steps
    assert "untouched" in out, "the deferral must say the edge was not touched"


def test_an_invalid_lock_inventory_never_touches_the_live_orchestrator(tmp_path):
    rc, out, steps = _run_bootstrap(tmp_path, seize_status=2)
    assert rc == 9, out
    assert "pause" not in steps, f"inventory failure must not pause the edge: {steps}"
    assert "untouched" in out


def test_the_seize_strictly_precedes_the_pause(tmp_path):
    rc, out, steps = _run_bootstrap(tmp_path)
    assert rc == 0, out
    assert "seize" in steps and "pause" in steps, steps
    assert steps.index("seize") < steps.index("pause"), (
        f"the lock seize must happen BEFORE the pause: {steps}"
    )
    assert steps.index("pause") < steps.index("kill"), steps
    assert steps.index("pause") < steps.index("audit"), (
        "the audit still runs against a frozen control"
    )


def test_an_operation_admitted_during_the_seize_window_defers(tmp_path):
    """Seizing first reopens a window the pause used to close; the census shuts it."""
    rc, out, steps = _run_bootstrap(tmp_path, census_drifts=True)
    assert rc == 9, out
    assert "admitted a new operation" in out
    assert "kill" not in steps, f"a drifted census must never reach the fatal stop: {steps}"
