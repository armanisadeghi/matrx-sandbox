"""Adversarial tests for the standalone EC2 root-copy helper."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import ec2_home_copy as copy
from orchestrator import ec2_home_copy_client as client


CID = "a" * 64
IMAGE = "sha256:" + "b" * 64


def operation(**changes):
    result = {"schema_version": 1, "operation": "12345678-1234-1234-1234-123456789abc", "source_container_id": CID, "source_image": IMAGE, "source_pid": 1234, "source_started_at": "2026-01-01T00:00:00Z", "source_overlay_identity": {"Name": "overlay2", "Data": {"UpperDir": "/trusted/upper"}}, "target_volume": "ec2-home-123", "target_created_at": "2026-01-01T00:00:00Z"}
    result.update(changes)
    return result


def source(paused=True, mounts=None, **changes):
    result = {"Id": CID, "Image": IMAGE, "State": {"Pid": 1234, "StartedAt": "2026-01-01T00:00:00Z", "Paused": paused}, "GraphDriver": {"Name": "overlay2", "Data": {"UpperDir": "/trusted/upper"}}, "Mounts": [] if mounts is None else mounts}
    result.update(changes)
    return result


def volume(path="/var/lib/docker/volumes/ec2-home-123/_data", **changes):
    result = {"Name": "ec2-home-123", "Driver": "local", "Scope": "local", "Options": None, "CreatedAt": "2026-01-01T00:00:00Z", "Labels": {"matrx.owner": "orchestrator", "matrx.ec2_home_copy": operation()["operation"]}, "Mountpoint": path}
    result.update(changes)
    return result


@pytest.mark.parametrize("destination", ["/", "/home", "/home/agent", "/home/agent/cache", "not-absolute"])
def test_rejects_every_home_ancestor_or_nested_mount(destination):
    with pytest.raises(copy.Ec2HomeCopyError, match="home ancestor"):
        copy.validate_source(source(mounts=[{"Destination": destination}]), operation())


def test_rejects_pid_container_overlay_and_pause_substitution():
    for inspected in [source(paused=False), source(State={"Pid": 9, "StartedAt": "2026-01-01T00:00:00Z", "Paused": True}), source(GraphDriver={"Name": "overlay2", "Data": {"UpperDir": "/other"}}), source(Id="c" * 64)]:
        with pytest.raises(copy.Ec2HomeCopyError):
            copy.validate_source(inspected, operation())


def test_rejects_target_substitution_and_unowned_volume():
    for inspected in [volume(Name="other"), volume(CreatedAt="later"), volume(Labels={}), volume(Driver="nfs")]:
        with pytest.raises(copy.Ec2HomeCopyError):
            copy.validate_target_volume(inspected, operation())


def test_proc_root_is_the_only_component_allowed_to_follow(monkeypatch):
    calls = []
    handles = iter([10, 11, 12])
    def opened(path, flags, **kwargs):
        calls.append((path, flags, kwargs)); return next(handles)
    monkeypatch.setattr(copy.os, "open", opened)
    monkeypatch.setattr(copy.os, "close", lambda fd: None)
    assert copy.source_home_fd(99) == 12
    assert not calls[0][1] & os.O_NOFOLLOW
    assert calls[1][1] & os.O_NOFOLLOW and calls[2][1] & os.O_NOFOLLOW


def test_secure_target_open_refuses_symlink(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises((copy.Ec2HomeCopyError, OSError)):
        copy._open_absolute_directory(str(link))


def test_perform_rechecks_after_stream_and_refuses_substitution(monkeypatch, tmp_path):
    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir(); target.mkdir(); (home / "a").write_text("source")
    inspections = iter([source(), volume(str(target)), source(GraphDriver={"Name": "overlay2", "Data": {"UpperDir": "/changed"}})])
    monkeypatch.setattr(copy, "_inspect", lambda *_: next(inspections))
    monkeypatch.setattr(copy, "source_home_fd", lambda _: os.open(home, os.O_RDONLY))
    monkeypatch.setattr(copy, "_open_absolute_directory", lambda _: os.open(target, os.O_RDONLY))
    monkeypatch.setattr(copy, "_manifest", lambda fd: {"entries": []})
    monkeypatch.setattr(copy, "_tar_copy", lambda _s, _t: None)
    with pytest.raises(copy.Ec2HomeCopyError, match="overlay"):
        copy._perform_copy_locked(operation())


def test_tar_failure_is_not_ignored(monkeypatch):
    class Failed:
        stdout = type("S", (), {"close": lambda self: None})()
        pid = 1
        def wait(self, **_): return 1
        def poll(self): return 1
    monkeypatch.setattr(copy.subprocess, "Popen", lambda *a, **k: Failed())
    with pytest.raises(copy.Ec2HomeCopyError, match="tar stream failed"):
        copy._tar_copy(0, 1)


@pytest.mark.skipif(not shutil.which("gtar"), reason="GNU tar is unavailable on this developer host")
def test_real_gnu_tar_fd_transport_preserves_content_and_hardlinks(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(Path(shutil.which("gtar")).parent) + os.pathsep + os.environ["PATH"])
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir(); target_dir.mkdir()
    (source_dir / "file").write_bytes(b"payload")
    os.link(source_dir / "file", source_dir / "linked")
    sparse = source_dir / "sparse"
    with open(sparse, "wb") as f: f.seek(1024 * 1024); f.write(b"x")
    sf, tf = os.open(source_dir, os.O_RDONLY), os.open(target_dir, os.O_RDONLY)
    try: copy._tar_copy(sf, tf)
    finally: os.close(sf); os.close(tf)
    assert (target_dir / "file").read_bytes() == b"payload"
    assert os.stat(target_dir / "file").st_ino == os.stat(target_dir / "linked").st_ino
    assert (target_dir / "sparse").stat().st_size == 1024 * 1024 + 1


def test_root_artifact_hash_verification_rejects_mutation(monkeypatch, tmp_path):
    digest = hashlib.sha256(b"release").hexdigest()
    monkeypatch.setattr(copy, "_LIBEXEC", str(tmp_path / "helper"))
    artifact = Path(copy.artifact_path(digest)); artifact.write_text("release")
    artifact.chmod(0o755)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    with pytest.raises(copy.Ec2HomeCopyError, match="root owned"):
        copy.verify_root_artifact(str(artifact), digest)


def test_root_copy_lock_is_reowned_to_journal_identity(monkeypatch):
    changed = []
    synced = []
    directory = SimpleNamespace(st_uid=1000, st_gid=1000)
    root_lock = SimpleNamespace(
        st_mode=0o100600, st_nlink=1, st_uid=0, st_gid=0,
        st_dev=1, st_ino=2, st_size=0,
    )
    service_lock = SimpleNamespace(
        st_mode=0o100600, st_nlink=1, st_uid=1000, st_gid=1000,
        st_dev=1, st_ino=2, st_size=0,
    )
    locks = iter((root_lock, service_lock))
    monkeypatch.setattr(copy.os, "fstat", lambda _fd: next(locks))
    monkeypatch.setattr(copy.os, "fchown", lambda fd, uid, gid: changed.append((fd, uid, gid)))
    monkeypatch.setattr(copy.os, "fsync", lambda fd: synced.append(fd))

    copy._normalize_copy_lock(17, directory, "copy-operation.lock")

    assert changed == [(17, 1000, 1000)]
    assert synced == [17]


def test_copy_lock_rejects_unrelated_owner(monkeypatch):
    directory = SimpleNamespace(st_uid=1000, st_gid=1000)
    foreign = SimpleNamespace(
        st_mode=0o100600, st_nlink=1, st_uid=2000, st_gid=2000,
        st_dev=1, st_ino=2, st_size=0,
    )
    monkeypatch.setattr(copy.os, "fstat", lambda _fd: foreign)

    with pytest.raises(copy.Ec2HomeCopyError, match="incompatible owner"):
        copy._normalize_copy_lock(17, directory, "copy-operation.lock")


def test_copy_lock_rejects_descriptor_substitution_after_reownership(monkeypatch):
    directory = SimpleNamespace(st_uid=1000, st_gid=1000)
    root_lock = SimpleNamespace(
        st_mode=0o100600, st_nlink=1, st_uid=0, st_gid=0,
        st_dev=1, st_ino=2, st_size=0,
    )
    substituted = SimpleNamespace(
        st_mode=0o100600, st_nlink=1, st_uid=1000, st_gid=1000,
        st_dev=1, st_ino=3, st_size=0,
    )
    locks = iter((root_lock, substituted))
    monkeypatch.setattr(copy.os, "fstat", lambda _fd: next(locks))
    monkeypatch.setattr(copy.os, "fchown", lambda *_args: None)
    monkeypatch.setattr(copy.os, "fsync", lambda *_args: None)

    with pytest.raises(copy.Ec2HomeCopyError, match="changed during ownership"):
        copy._normalize_copy_lock(17, directory, "copy-1234567890abcdef.lock")


def test_normalizer_rejects_named_inode_replacement_during_scan(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    lock = root / "copy-1234567890abcdef.lock"
    lock.write_bytes(b"original")
    lock.chmod(0o600)
    real_stat = copy.os.stat
    named_stats = 0

    def replace_before_final_named_stat(path, *args, **kwargs):
        nonlocal named_stats
        if path == lock.name and kwargs.get("dir_fd") is not None:
            named_stats += 1
            if named_stats == 2:
                lock.unlink()
                lock.write_bytes(b"replacement")
                lock.chmod(0o600)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(copy.os, "stat", replace_before_final_named_stat)

    with pytest.raises(copy.Ec2HomeCopyError, match="name changed during normalization"):
        copy.normalize_copy_locks(
            str(root), expected_uid=os.geteuid(), expected_gid=os.getegid()
        )
    assert lock.read_bytes() == b"replacement"


def test_normalizer_rejects_journal_directory_replacement(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    displaced = tmp_path / "displaced"
    root.mkdir(mode=0o700)
    real_stat = copy.os.stat
    root_stats = 0

    def replace_before_final_directory_stat(path, *args, **kwargs):
        nonlocal root_stats
        if path == str(root) and kwargs.get("dir_fd") is None:
            root_stats += 1
            if root_stats == 2:
                root.rename(displaced)
                root.mkdir(mode=0o700)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(copy.os, "stat", replace_before_final_directory_stat)

    with pytest.raises(copy.Ec2HomeCopyError, match="directory changed"):
        copy.normalize_copy_locks(
            str(root), expected_uid=os.geteuid(), expected_gid=os.getegid()
        )


def _prepare_perform_copy_lock_test(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(copy, "_JOURNAL_DIR", str(root))
    entered = []
    monkeypatch.setattr(copy, "_perform_copy_locked", lambda op: entered.append(op))
    return root, entered


def test_perform_copy_rejects_lock_name_replacement_during_normalization(
    tmp_path, monkeypatch
):
    root, entered = _prepare_perform_copy_lock_test(tmp_path, monkeypatch)
    flocked = []
    real_normalize = copy._normalize_copy_lock

    def replace_name(fd, directory, name):
        real_normalize(fd, directory, name)
        lock = root / name
        lock.unlink()
        lock.write_bytes(b"replacement")
        lock.chmod(0o600)

    monkeypatch.setattr(copy, "_normalize_copy_lock", replace_name)
    monkeypatch.setattr(copy.fcntl, "flock", lambda *_args: flocked.append(True))

    with pytest.raises(copy.Ec2HomeCopyError, match="name changed"):
        copy.perform_copy(operation())
    assert flocked == []
    assert entered == []


def test_perform_copy_rejects_lock_name_replacement_after_flock(
    tmp_path, monkeypatch
):
    root, entered = _prepare_perform_copy_lock_test(tmp_path, monkeypatch)
    real_flock = copy.fcntl.flock

    def replace_name_after_flock(fd, flags):
        real_flock(fd, flags)
        name = "copy-" + operation()["operation"] + ".lock"
        lock = root / name
        lock.unlink()
        lock.write_bytes(b"replacement")
        lock.chmod(0o600)

    monkeypatch.setattr(copy.fcntl, "flock", replace_name_after_flock)

    with pytest.raises(copy.Ec2HomeCopyError, match="name changed"):
        copy.perform_copy(operation())
    assert entered == []


def test_perform_copy_rejects_journal_replacement_after_flock(
    tmp_path, monkeypatch
):
    root, entered = _prepare_perform_copy_lock_test(tmp_path, monkeypatch)
    displaced = tmp_path / "displaced"
    real_flock = copy.fcntl.flock

    def replace_journal_after_flock(fd, flags):
        real_flock(fd, flags)
        root.rename(displaced)
        root.mkdir(mode=0o700)

    monkeypatch.setattr(copy.fcntl, "flock", replace_journal_after_flock)

    with pytest.raises(copy.Ec2HomeCopyError, match="directory changed"):
        copy.perform_copy(operation())
    assert entered == []


@pytest.mark.skipif(sys.platform != "linux", reason="two-UID ownership proof is Linux-only")
def test_real_copy_lock_normalizer_changes_only_canonical_root_artifacts(tmp_path):
    """Normalize only the real helper class while preserving inode and lock semantics."""
    service_uid = os.geteuid() or 65534
    service_gid = os.getegid() or 65534
    privileged_python = [sys.executable]
    if os.geteuid() != 0:
        assert shutil.which("sudo"), "Linux two-UID proof requires sudo when pytest is non-root"
        sudo = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, check=False
        )
        assert sudo.returncode == 0, "Linux two-UID proof requires passwordless sudo"
        privileged_python = ["sudo", "-n", sys.executable]

    script = textwrap.dedent(
        r"""
        import fcntl
        import importlib.util
        import os
        import stat
        import sys

        module_path, base, service_uid, service_gid = sys.argv[1:]
        service_uid, service_gid = int(service_uid), int(service_gid)
        spec = importlib.util.spec_from_file_location("ec2_home_copy_probe", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def directory(name, mode=0o700):
            root = os.path.join(base, name)
            os.mkdir(root)
            os.chown(root, service_uid, service_gid)
            os.chmod(root, mode)
            return root

        def root_file(root, name, *, mode=0o600, content=b"lock-evidence"):
            path = os.path.join(root, name)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, mode)
            os.write(fd, content)
            os.fsync(fd)
            return path, fd

        valid_root = directory("valid")
        valid_path, owner_fd = root_file(
            valid_root, "copy-12345678-1234-1234-1234-123456789abc.lock"
        )
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = os.fstat(owner_fd)
        module.normalize_copy_locks(
            valid_root, expected_uid=service_uid, expected_gid=service_gid
        )
        after = os.stat(valid_path, follow_symlinks=False)
        assert (after.st_dev, after.st_ino, after.st_size) == (
            before.st_dev, before.st_ino, before.st_size
        )
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            service_uid, service_gid, 0o600
        )
        with open(valid_path, "rb") as value:
            assert value.read() == b"lock-evidence"
        competitor = os.open(valid_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("normalization released the active flock")
        finally:
            os.close(competitor)
            os.close(owner_fd)

        broad_root = directory("broad")
        broad_path, broad_fd = root_file(broad_root, "copy-operation.lock")
        os.close(broad_fd)
        module.normalize_copy_locks(
            broad_root, expected_uid=service_uid, expected_gid=service_gid
        )
        assert os.stat(broad_path).st_uid == 0

        bad_mode_root = directory("bad-mode")
        bad_mode_path, bad_mode_fd = root_file(
            bad_mode_root, "copy-1234567890abcdef.lock", mode=0o640
        )
        os.close(bad_mode_fd)
        try:
            module.normalize_copy_locks(
                bad_mode_root, expected_uid=service_uid, expected_gid=service_gid
            )
        except module.Ec2HomeCopyError as exc:
            assert "mode" in str(exc)
        else:
            raise AssertionError("bad lock mode was normalized")
        assert os.stat(bad_mode_path).st_uid == 0

        hardlink_root = directory("hardlink")
        hardlink_path, hardlink_fd = root_file(
            hardlink_root, "copy-fedcba9876543210.lock"
        )
        os.close(hardlink_fd)
        os.link(hardlink_path, os.path.join(hardlink_root, "alias"))
        try:
            module.normalize_copy_locks(
                hardlink_root, expected_uid=service_uid, expected_gid=service_gid
            )
        except module.Ec2HomeCopyError as exc:
            assert "regular inode" in str(exc)
        else:
            raise AssertionError("hard-linked lock was normalized")
        assert os.stat(hardlink_path).st_uid == 0

        symlink_root = directory("symlink")
        target_path, target_fd = root_file(symlink_root, "target")
        os.close(target_fd)
        symlink_path = os.path.join(symlink_root, "copy-abcdef1234567890.lock")
        os.symlink(target_path, symlink_path)
        try:
            module.normalize_copy_locks(
                symlink_root, expected_uid=service_uid, expected_gid=service_gid
            )
        except module.Ec2HomeCopyError as exc:
            assert "cannot safely open" in str(exc)
        else:
            raise AssertionError("symlink lock was normalized")
        assert os.lstat(symlink_path).st_uid == 0

        foreign_root = directory("foreign")
        foreign_path, foreign_fd = root_file(
            foreign_root, "copy-0123456789abcdef.lock"
        )
        os.fchown(foreign_fd, service_uid + 10000, service_gid + 10000)
        os.close(foreign_fd)
        try:
            module.normalize_copy_locks(
                foreign_root, expected_uid=service_uid, expected_gid=service_gid
            )
        except module.Ec2HomeCopyError as exc:
            assert "owner" in str(exc)
        else:
            raise AssertionError("foreign-owned lock was normalized")
        assert os.stat(foreign_path).st_uid == service_uid + 10000

        wrong_identity_root = directory("wrong-identity")
        wrong_path, wrong_fd = root_file(
            wrong_identity_root, "copy-aabbccddeeff0011.lock"
        )
        os.close(wrong_fd)
        try:
            module.normalize_copy_locks(
                wrong_identity_root,
                expected_uid=service_uid + 1,
                expected_gid=service_gid,
            )
        except module.Ec2HomeCopyError as exc:
            assert "expected service user" in str(exc)
        else:
            raise AssertionError("wrong journal identity was trusted")
        assert os.stat(wrong_path).st_uid == 0

        bad_directory = directory("bad-directory", mode=0o750)
        bad_directory_path, bad_directory_fd = root_file(
            bad_directory, "copy-1122334455667788.lock"
        )
        os.close(bad_directory_fd)
        try:
            module.normalize_copy_locks(
                bad_directory, expected_uid=service_uid, expected_gid=service_gid
            )
        except module.Ec2HomeCopyError as exc:
            assert "directory mode" in str(exc)
        else:
            raise AssertionError("unsafe journal mode was trusted")
        assert os.stat(bad_directory_path).st_uid == 0
        """
    )
    completed = subprocess.run(
        [
            *privileged_python, "-I", "-c", script,
            str(Path(copy.__file__).resolve()), str(tmp_path),
            str(service_uid), str(service_gid),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_preflight_passes_the_actual_service_identity(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"EC2_HOME_COPY_READY\n", b""

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(client, "_artifact_digest", lambda: "a" * 64)
    monkeypatch.setattr(client, "artifact_path", lambda _digest: "/approved/helper.py")
    monkeypatch.setattr(client, "verify_root_artifact", lambda *_args: None)
    monkeypatch.setattr(client.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(client.os, "getegid", lambda: 5678)
    monkeypatch.setattr(client.asyncio, "create_subprocess_exec", spawn)

    await client.preflight_helper()

    assert calls[0][0][-3:] == ("--preflight", "1234", "5678")
