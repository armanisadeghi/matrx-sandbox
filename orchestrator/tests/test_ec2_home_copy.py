"""Adversarial tests for the standalone EC2 root-copy helper."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator import ec2_home_copy as copy


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
