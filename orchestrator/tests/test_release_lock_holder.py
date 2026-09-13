from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import sys

import pytest

from orchestrator import release_lock_holder as holder_module
from orchestrator.release_lock_holder import (
    LockContentionError,
    LockInventoryError,
    LockLease,
)


def test_lock_lease_preserves_bytes_without_truncation(tmp_path):
    deployment = tmp_path / "deployment.lock"
    deployment.write_bytes(b"deployment-bytes")
    deployment.chmod(0o600)
    operation = tmp_path / "sbx-one.lock"
    operation.write_bytes(b"operation-bytes")
    operation.chmod(0o600)
    lease = LockLease(tmp_path)
    try:
        receipt = lease.acquire(["deployment", "sbx-one"])
        assert receipt["locks"] == 2
    finally:
        lease.close()

    assert deployment.read_bytes() == b"deployment-bytes"
    assert operation.read_bytes() == b"operation-bytes"


def test_bootstrap_exclusive_census_rejects_ordinary_shared_owner(tmp_path):
    operation = tmp_path / "sbx-one.lock"
    operation.touch(mode=0o600)
    with operation.open("r+b") as ordinary:
        fcntl.flock(ordinary, fcntl.LOCK_SH | fcntl.LOCK_NB)
        lease = LockLease(tmp_path)
        with pytest.raises(LockContentionError, match="already owned"):
            lease.acquire(["deployment"], include_existing=True)
        lease.close()


def test_bootstrap_exclusive_census_rejects_exclusive_migration_owner(tmp_path):
    operation = tmp_path / "sbx-one.lock"
    operation.touch(mode=0o600)
    with operation.open("r+b") as migration:
        fcntl.flock(migration, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lease = LockLease(tmp_path)
        with pytest.raises(LockContentionError, match="already owned"):
            lease.acquire(["deployment"], include_existing=True)
        lease.close()


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink"])
def test_lock_lease_rejects_every_nonregular_lock_entry(tmp_path, kind):
    path = tmp_path / "foreign.lock"
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o600)
    else:
        target = tmp_path / "outside"
        target.write_bytes(b"do-not-touch")
        path.symlink_to(target)

    lease = LockLease(tmp_path)
    with pytest.raises(LockInventoryError, match="not a regular file|safely pin"):
        lease.acquire(["deployment"], include_existing=True)
    lease.close()


@pytest.mark.parametrize("replacement", ["regular", "fifo", "symlink"])
def test_lock_lease_rejects_name_to_inode_replacement(tmp_path, monkeypatch, replacement):
    path = tmp_path / "sbx-one.lock"
    path.write_bytes(b"original")
    path.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    lease = LockLease(tmp_path)
    original = lease._open_and_lock
    replaced = False
    pinned_bytes = []

    if replacement == "regular":
        # Force the oracle to ignore every timestamp/size field. The retained
        # descriptor itself must prevent inode-number reuse and catch the ABA.
        monkeypatch.setattr(
            holder_module, "_identity", lambda value: (value.st_dev, value.st_ino)
        )

    def replace_before_descriptor(name, expected, fd):
        nonlocal replaced
        if name == "sbx-one.lock" and not replaced:
            pinned_bytes.append(os.pread(fd, 64, 0))
            replaced = True
            path.unlink()
            if replacement == "regular":
                path.write_bytes(b"replacement")
                path.chmod(0o600)
            elif replacement == "fifo":
                os.mkfifo(path, 0o600)
            else:
                path.symlink_to(outside)
        return original(name, expected, fd)

    monkeypatch.setattr(lease, "_open_and_lock", replace_before_descriptor)
    with pytest.raises(
        LockInventoryError,
        match="changed|safely open|not a regular|unexpected hard links",
    ):
        lease.acquire(["deployment"], include_existing=True)
    lease.close()
    assert pinned_bytes == [b"original"]
    assert outside.read_bytes() == b"outside"


def test_steady_state_named_lease_does_not_census_unrelated_operation_locks(tmp_path):
    os.mkfifo(tmp_path / "foreign.lock", 0o600)
    lease = LockLease(tmp_path)
    try:
        receipt = lease.acquire(["deployment"])
        assert receipt["locks"] == 1
    finally:
        lease.close()


def test_lock_lease_rejects_journal_directory_inode_replacement(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    lease = LockLease(journal)
    lease._directory()
    moved = tmp_path / "moved"
    journal.rename(moved)
    journal.mkdir()
    with pytest.raises(LockInventoryError, match="directory inode changed"):
        lease.acquire(["deployment"], include_existing=True)
    lease.close()


def test_lock_lease_refuses_incompatible_existing_mode_without_repair(tmp_path):
    lock = tmp_path / "deployment.lock"
    lock.touch(mode=0o640)
    lease = LockLease(tmp_path)
    with pytest.raises(LockInventoryError, match="incompatible mode"):
        lease.acquire(["deployment"])
    lease.close()
    assert lock.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.geteuid() != 0, reason="requires two real Unix identities")
def test_service_identity_creates_locks_it_can_reopen_after_root_deploy(tmp_path):
    service_uid = 65534
    service_gid = 65534
    os.chown(tmp_path, service_uid, service_gid)
    tmp_path.chmod(0o700)
    helper = Path(__file__).parents[1] / "orchestrator" / "release_lock_holder.py"

    def become_service():
        os.setgroups([])
        os.setgid(service_gid)
        os.setuid(service_uid)

    holder = subprocess.Popen(
        [sys.executable, str(helper), str(tmp_path), "deployment", "sbx-derived"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        preexec_fn=become_service,
    )
    assert holder.stdout is not None
    assert '"status": "ready"' in holder.stdout.readline()
    assert holder.stdin is not None
    holder.stdin.close()
    assert holder.wait(timeout=5) == 0

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sys; [os.close(os.open(p,os.O_RDWR|os.O_NOFOLLOW)) for p in sys.argv[1:]]",
            str(tmp_path / "deployment.lock"),
            str(tmp_path / "sbx-derived.lock"),
        ],
        preexec_fn=become_service,
        check=False,
    )
    assert probe.returncode == 0
    for name in ("deployment.lock", "sbx-derived.lock"):
        value = (tmp_path / name).stat()
        assert (value.st_uid, value.st_gid, value.st_mode & 0o777) == (
            service_uid,
            service_gid,
            0o600,
        )
