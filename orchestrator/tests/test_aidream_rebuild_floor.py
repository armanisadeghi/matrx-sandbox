"""The aidream TEMPLATE rebuild floor, executed as the real deploy-hosted.sh bash.

Before this floor existed, every 2-minute poller tick that saw a new aidream
commit started a ~6 GB, ~3-minute template build (~40 in 12 h on 2026-09-14),
and each completed one took the release promotion barrier that stops and
recreates the single-replica live orchestrator. Sustained build I/O is what
removed that orchestrator from the edge on 2026-09-13.

These tests run the real functions out of the real script — no reimplementation,
no asserting on source text for behavior — with only `docker`, `git` and `log`
stubbed, so they go red the moment the floor stops holding.
"""
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy-hosted.sh"

FUNCS = ("resolve_aidream_source_sha", "aidream_stale",
         "aidream_rebuild_floor_remaining", "stamp_aidream_rebuild")

BAKED = "b" * 40
MOVED = "a" * 40


def _extract(name: str) -> str:
    source = SCRIPT.read_text()
    marker = f"\n{name}() {{"
    assert marker in source, f"{name} is missing from {SCRIPT}"
    body = source.split(marker, 1)[1].split("\n}\n", 1)[0]
    return f"{name}() {{{body}\n}}\n"


def _run(tmp_path, *, baked=BAKED, remote=MOVED, stamp=None, interval=None):
    stamp_file = tmp_path / "aidream-build-epoch"
    if stamp is not None:
        stamp_file.write_text(f"{stamp}\n")
    harness = f'''
set -uo pipefail
AIDREAM_REBUILD_MIN_INTERVAL_SECONDS={21600 if interval is None else interval}
AIDREAM_REBUILD_STAMP="{stamp_file}"
AIDREAM_SRC_DIR=/unused
log() {{ printf '%s\\n' "$*"; }}
docker() {{
  case "$*" in
    *"Config.Labels"*) printf '%s\\n' "{baked}" ;;
    *) return 0 ;;
  esac
}}
git() {{ printf '%s\\trefs/heads/main\\n' "{remote}"; }}
'''
    script = harness + "".join(_extract(f) for f in FUNCS) + '\naidream_stale; echo "RC=$?"\n'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            env={**os.environ})
    assert result.returncode == 0, result.stderr
    rc = int(result.stdout.strip().rsplit("RC=", 1)[1])
    return rc, result.stdout, stamp_file


def test_moved_repo_inside_the_floor_defers_the_rebuild(tmp_path):
    rc, out, _ = _run(tmp_path, stamp=int(time.time()) - 600)
    assert rc == 1, f"a 10-minute-old template build must not rebuild again:\n{out}"
    assert "DEFERRED" in out and "AIDREAM_REBUILD_MIN_INTERVAL_SECONDS" in out
    assert "FORCE=1" in out, "a deferral must name its remedy"


def test_moved_repo_past_the_floor_rebuilds(tmp_path):
    rc, out, _ = _run(tmp_path, stamp=int(time.time()) - 25200)  # 7 h
    assert rc == 0, f"past the 6 h floor the template must refresh:\n{out}"
    assert "rebuild queued" in out


def test_floor_fails_open_with_no_stamp(tmp_path):
    rc, out, _ = _run(tmp_path, stamp=None)
    assert rc == 0 and "rebuild queued" in out


def test_corrupt_or_future_stamp_fails_open(tmp_path):
    assert _run(tmp_path, stamp=None)[0] == 0
    stamp_file = tmp_path / "aidream-build-epoch"
    stamp_file.write_text("not-a-number\n")
    assert _run(tmp_path)[0] == 0, "a corrupt stamp must never block a rebuild"
    stamp_file.write_text(f"{int(time.time()) + 86400}\n")
    assert _run(tmp_path)[0] == 0, "a stamp from the future must never block a rebuild"


def test_zero_disables_the_floor(tmp_path):
    rc, out, _ = _run(tmp_path, stamp=int(time.time()), interval=0)
    assert rc == 0 and "rebuild queued" in out


def test_current_image_never_rebuilds_regardless_of_the_floor(tmp_path):
    assert _run(tmp_path, remote=BAKED, stamp=None)[0] == 1
    assert _run(tmp_path, remote=BAKED, stamp=int(time.time()))[0] == 1


def test_unknown_remote_still_rebuilds_loudly(tmp_path):
    rc, out, _ = _run(tmp_path, remote="", stamp=int(time.time()))
    assert rc == 0, "the floor must never silence the freshness-unknown path"
    assert "UNKNOWN" in out


def test_the_build_site_stamps_before_building(tmp_path):
    """A stamp written only after a success would re-fire a failing 6 GB build every tick."""
    source = SCRIPT.read_text()
    build = source.index("build_candidate matrx-sandbox:aidream")
    preceding = source[:build].rsplit("\n", 3)[-3:]
    assert any("stamp_aidream_rebuild" in line for line in preceding), \
        "stamp_aidream_rebuild must run immediately before the aidream build"
