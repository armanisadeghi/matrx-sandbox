"""A cold mount left behind by a dead container must not poison the next boot.

The regression, in full (feedback 81cb4265, measured 2026-09-18): the first
aidream box for a user died at a later boot step without running
``shutdown.sh``, so its ``/data/cold`` FUSE mount survived on the user's
persistent volume. ``cold-mount.sh mount`` then ran ``mount-s3`` unconditionally,
``mount-s3`` exited non-zero with ``mount point /data/cold is already mounted``,
and ``set -euo pipefail`` in both ``cold-mount.sh`` and its caller
``entrypoint.sh`` killed the boot at step ``[2/5]``. Box ``sbx-e6a8dcdddd64``
exited 1 that way, and so did every later start for that user: one failed box
made the account unusable with the real cause buried in the docker log.

These are the three states a mount point can be in when we arrive.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "sandbox-image" / "scripts" / "cold-mount.sh"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def _fixture(
    tmp_path: Path,
    *,
    already_mounted: bool,
    serving: bool,
    unmount_works: bool = True,
    mount_s3_exit: int = 0,
) -> tuple[Path, Path]:
    """A stub world. ``mount-s3`` records its invocations so a test can prove
    whether we re-mounted, adopted, or recovered."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    mounted_flag = state / "mounted"
    if already_mounted:
        mounted_flag.write_text("1")

    # `mountpoint -q <path>`: exit 0 when the flag file exists.
    _stub(bin_dir, "mountpoint", f'test -f "{mounted_flag}"')
    # A serving mount lists fine; a dead FUSE endpoint fails like ENOTCONN.
    _stub(
        bin_dir,
        "ls",
        f'if [ -f "{mounted_flag}" ] && [ ! -f "{state}/serving" ]; then\n'
        "  echo 'ls: cannot open directory: Transport endpoint is not connected' >&2; exit 2\n"
        f"fi\nexec /bin/ls \"$@\"",
    )
    if serving:
        (state / "serving").write_text("1")
    _stub(
        bin_dir,
        "umount",
        (f'rm -f "{mounted_flag}"; exit 0' if unmount_works else "exit 1"),
    )
    _stub(
        bin_dir,
        "mount-s3",
        f'echo "$@" >> "{state}/mount-s3.calls"\n'
        + (
            f'test {mount_s3_exit} -eq 0 && touch "{mounted_flag}"\n'
            f"exit {mount_s3_exit}"
        ),
    )
    return bin_dir, state


def _run(tmp_path: Path, bin_dir: Path, action: str = "mount") -> subprocess.CompletedProcess[str]:
    cold = tmp_path / "cold"
    cold.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(SCRIPT), action],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "S3_BUCKET": "matrx-sandbox-storage-prod-2024",
            "USER_ID": "4a27bab9-fba3-46d4-86a5-e5d70f5bd9b7",
            "COLD_PATH": str(cold),
            "COLD_MOUNT_LOG_FILE": str(tmp_path / "cold-mount.log"),
        },
    )


def test_a_serving_mount_is_adopted_not_remounted(tmp_path: Path) -> None:
    """THE regression. This is the state box sbx-e6a8dcdddd64 booted into."""
    bin_dir, state = _fixture(tmp_path, already_mounted=True, serving=True)
    result = _run(tmp_path, bin_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "adopting it" in result.stdout
    # The whole point: mount-s3 is never invoked, so it can never exit 1 with
    # "already mounted" and take the boot down with it.
    assert not (state / "mount-s3.calls").exists(), result.stdout


def test_a_dead_fuse_endpoint_is_cleared_and_remounted(tmp_path: Path) -> None:
    bin_dir, state = _fixture(tmp_path, already_mounted=True, serving=False)
    result = _run(tmp_path, bin_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not serving" in result.stdout
    assert "Stale mount cleared" in result.stdout
    assert (state / "mount-s3.calls").exists(), "a dead mount must be replaced"
    assert "Cold storage mounted successfully" in result.stdout


def test_an_unclearable_mount_refuses_with_a_remedy(tmp_path: Path) -> None:
    # Never a silent pass: if we cannot clear it we cannot serve cold storage,
    # and the sentence has to say what to do instead of naming mount-s3's error.
    bin_dir, state = _fixture(
        tmp_path, already_mounted=True, serving=False, unmount_works=False
    )
    result = _run(tmp_path, bin_dir)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "could not clear the stale mount" in result.stdout
    assert "REMEDY" in result.stdout
    assert not (state / "mount-s3.calls").exists()


def test_an_unmounted_path_mounts_exactly_as_before(tmp_path: Path) -> None:
    bin_dir, state = _fixture(tmp_path, already_mounted=False, serving=False)
    result = _run(tmp_path, bin_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "adopting it" not in result.stdout
    calls = (state / "mount-s3.calls").read_text()
    assert "--prefix users/4a27bab9-fba3-46d4-86a5-e5d70f5bd9b7/cold/" in calls
    assert "Cold storage mounted successfully" in result.stdout


def test_a_real_mount_failure_still_fails(tmp_path: Path) -> None:
    # The idempotency branch must not become a blanket swallow of mount errors.
    bin_dir, _ = _fixture(
        tmp_path, already_mounted=False, serving=False, mount_s3_exit=1
    )
    result = _run(tmp_path, bin_dir)
    assert result.returncode != 0, result.stdout + result.stderr
