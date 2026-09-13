"""Forcing harness for the EC2 bootstrap stop: no host/systemd access required."""

from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def _function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    depth = 0
    for index in range(start, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[start : index + 1]
    raise AssertionError(f"unterminated {name}")


def test_empty_bootstrap_lock_array_does_not_close_diagnostics(tmp_path):
    source = (ROOT / "scripts" / "deploy-ec2.sh").read_text()
    restore = "\n\n".join(
        _function(source, name)
        for name in ("release_migration_lock_holder", "restore_bootstrap_runtime")
    )
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.freeze").write_text("1\n")
    (cgroup / "cgroup.events").write_text("frozen 0\n")
    dropin = tmp_path / "bootstrap.conf"
    dropin.write_text("[Service]\nRestart=no\n")
    harness = f'''set -euo pipefail
systemctl() {{ return 0; }}
{restore}
BOOTSTRAP_ACTIVE=1
BOOTSTRAP_CGROUP="{cgroup}"
BOOTSTRAP_PID=$$
MIGRATION_LOCK_HOLDER_PID=""
MIGRATION_LOCK_HOLDER_INPUT_FD=""
MIGRATION_LOCK_HOLDER_OUTPUT_FD=""
RUNTIME_DROPIN="{dropin}"
UNIT=matrx-orchestrator
restore_bootstrap_runtime
echo diagnostics-still-visible
'''
    bash = "/opt/homebrew/bin/bash" if Path("/opt/homebrew/bin/bash").exists() else shutil.which("bash")
    assert bash is not None

    result = subprocess.run([bash, "-c", harness], text=True, capture_output=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "diagnostics-still-visible\n"


def test_bootstrap_freezes_cgroup_locks_then_kills_without_systemctl_stop(tmp_path):
    """Mutant: replacing cgroup.kill with systemctl stop fails the observable order."""
    source = (ROOT / "scripts" / "deploy-ec2.sh").read_text()
    functions = "\n\n".join(
        _function(source, name)
        for name in (
            "release_barrier_audit",
            "release_migration_lock_holder",
            "restore_bootstrap_runtime",
            "bootstrap_fail",
            "bootstrap_stop_old_orchestrator",
        )
    )
    cgroup = tmp_path / "cgroup" / "matrx.service"
    cgroup.mkdir(parents=True)
    for name, value in {
        "cgroup.freeze": "0\n",
        "cgroup.kill": "",
        "cgroup.events": "frozen 1\n",
        "cgroup.procs": "4242\n4243\n",
    }.items():
        (cgroup / name).write_text(value)
    journal = tmp_path / "journal"
    journal.mkdir()
    (journal / "sandbox-a.lock").touch()
    runtime_dropin = tmp_path / "run" / "matrx.conf"
    log = tmp_path / "commands"
    harness = f'''set -euo pipefail
fail() {{ echo "FAIL:$*" >&2; return 99; }}
log() {{ :; }}
systemctl() {{
  echo "systemctl $*" >> "{log}"
  case "$*" in
    *"ControlGroup"*) echo /matrx.service ;;
    *"Restart"*) echo no ;;
    *"MainPID"*) echo 4242 ;;
    *"is-active"*) return 1 ;;
  esac
}}
systemd-run() {{ echo "systemd-run $*" >> "{log}"; return 0; }}
sudo() {{ echo "audit $*" >> "{log}"; return 0; }}
bootstrap_old_lock_census() {{ echo "lock-holder exclusive" >> "{log}"; return 0; }}
printf() {{
  if [ "${{@: -1}}" = "$BOOTSTRAP_CGROUP/cgroup.kill" ]; then
    command printf "$@"
    : > "$BOOTSTRAP_CGROUP/cgroup.procs"
    return
  fi
  if [ "${{@: -1}}" = "$BOOTSTRAP_CGROUP/cgroup.freeze" ]; then
    command printf "$@"
    case "$1" in
      *1*) command printf 'frozen 1\\n' > "$BOOTSTRAP_CGROUP/cgroup.events" ;;
      *0*) command printf 'frozen 0\\n' > "$BOOTSTRAP_CGROUP/cgroup.events" ;;
    esac
  else
    command printf "$@"
  fi
}}
cat() {{
  if [ "$1" = "$BOOTSTRAP_CGROUP/cgroup.procs" ] && [ -s "$BOOTSTRAP_CGROUP/cgroup.kill" ]; then
    return 0
  fi
  command cat "$@"
}}
kill() {{
  if [ "$1" = -0 ] && [ "$2" = 4242 ] && [ -s "$BOOTSTRAP_CGROUP/cgroup.kill" ]; then
    return 1
  fi
  command kill "$@"
}}
{functions}
JOURNAL_DIR="{journal}"
CANDIDATE_DIR="{tmp_path}/candidate"
UNIT=matrx-orchestrator
CGROUP_ROOT="{tmp_path}/cgroup"
RUNTIME_DROPIN_DIR="{runtime_dropin.parent}"
RUNTIME_DROPIN="{runtime_dropin}"
BOOTSTRAP_ACTIVE=0
BOOTSTRAP_STOPPED=0
BOOTSTRAP_KILLED=0
BOOTSTRAP_CGROUP=""
BOOTSTRAP_PID=""
BOOTSTRAP_PROCS=""
MIGRATION_LOCK_HOLDER_PID=""
MIGRATION_LOCK_HOLDER_INPUT_FD=""
MIGRATION_LOCK_HOLDER_OUTPUT_FD=""
    TARGET_SHA=0000000000000000000000000000000000000000
mkdir -p "$CANDIDATE_DIR/.venv/bin"
touch "$CANDIDATE_DIR/.venv/bin/python"
bootstrap_stop_old_orchestrator
test "$BOOTSTRAP_STOPPED" = 1
test "$(cat "{cgroup}/cgroup.freeze")" = 1
test "$(cat "{cgroup}/cgroup.kill")" = 1
! grep -q "systemctl stop" "{log}"
grep -q "lock-holder exclusive" "{log}"
'''
    bash = "/opt/homebrew/bin/bash" if Path("/opt/homebrew/bin/bash").exists() else shutil.which("bash")
    assert bash is not None
    result = subprocess.run([bash, "-c", harness], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_bootstrap_lock_contention_thaws_and_removes_restart_dropin(tmp_path):
    """A held old migration lock must defer cleanly, not strand the old unit frozen."""
    source = (ROOT / "scripts" / "deploy-ec2.sh").read_text()
    functions = "\n\n".join(
        _function(source, name)
        for name in (
            "release_barrier_audit",
            "release_migration_lock_holder",
            "restore_bootstrap_runtime",
            "bootstrap_fail",
            "bootstrap_stop_old_orchestrator",
        )
    )
    cgroup = tmp_path / "cgroup" / "matrx.service"
    cgroup.mkdir(parents=True)
    for name, value in {
        "cgroup.freeze": "0\n",
        "cgroup.kill": "",
        "cgroup.events": "frozen 1\n",
        "cgroup.procs": "4242\n",
    }.items():
        (cgroup / name).write_text(value)
    journal = tmp_path / "journal"
    journal.mkdir()
    (journal / "sandbox-a.lock").touch()
    runtime_dropin = tmp_path / "run" / "matrx.conf"
    harness = f'''set -euo pipefail
fail() {{ echo "FAIL:$*" >&2; return 99; }}
log() {{ :; }}
systemctl() {{
  case "$*" in
    *"ControlGroup"*) echo /matrx.service ;;
    *"Restart"*) echo no ;;
    *"MainPID"*) echo 4242 ;;
    *"is-active"*) return 0 ;;
  esac
}}
systemd-run() {{ return 0; }}
sudo() {{ return 0; }}
bootstrap_old_lock_census() {{ bootstrap_fail "lock contention"; }}
printf() {{
  if [ "${{@: -1}}" = "$BOOTSTRAP_CGROUP/cgroup.freeze" ]; then
    command printf "$@"
    case "$1" in
      *1*) command printf 'frozen 1\\n' > "$BOOTSTRAP_CGROUP/cgroup.events" ;;
      *0*) command printf 'frozen 0\\n' > "$BOOTSTRAP_CGROUP/cgroup.events" ;;
    esac
  else
    command printf "$@"
  fi
}}
cat() {{ command cat "$@"; }}
{functions}
JOURNAL_DIR="{journal}"
CANDIDATE_DIR="{tmp_path}/candidate"
UNIT=matrx-orchestrator
CGROUP_ROOT="{tmp_path}/cgroup"
RUNTIME_DROPIN_DIR="{runtime_dropin.parent}"
RUNTIME_DROPIN="{runtime_dropin}"
BOOTSTRAP_ACTIVE=0
BOOTSTRAP_STOPPED=0
BOOTSTRAP_KILLED=0
BOOTSTRAP_CGROUP=""
BOOTSTRAP_PID=""
BOOTSTRAP_PROCS=""
MIGRATION_LOCK_HOLDER_PID=""
MIGRATION_LOCK_HOLDER_INPUT_FD=""
MIGRATION_LOCK_HOLDER_OUTPUT_FD=""
TARGET_SHA=0000000000000000000000000000000000000000
mkdir -p "$CANDIDATE_DIR/.venv/bin"
touch "$CANDIDATE_DIR/.venv/bin/python"
if bootstrap_stop_old_orchestrator; then exit 88; fi
test "$(cat "{cgroup}/cgroup.freeze")" = 0
test ! -e "$RUNTIME_DROPIN"
test ! -s "{cgroup}/cgroup.kill"
'''
    bash = "/opt/homebrew/bin/bash" if Path("/opt/homebrew/bin/bash").exists() else shutil.which("bash")
    assert bash is not None
    result = subprocess.run([bash, "-c", harness], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
