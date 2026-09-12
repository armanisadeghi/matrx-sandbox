"""Forcing guards for hosted_backup's Docker boundary.

The SUT owns receipt validation and destructive ordering; Docker is the only
double.  Each manifest is an independently authored external helper result.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from docker.errors import ContainerError, NotFound

from orchestrator.hosted_backup import HostedBackupError, restore_volume, snapshot_volume


IMAGE = "sha256:" + "a" * 64
SOURCE = "owned-source-9e7d"
BACKUP = "owned-backup-9e7d"
MANIFEST_A = {"entries": [{"path": ".", "uid": 101, "gid": 102, "mode": 493, "acl": "user::rwx\n", "xattrs": {}, "type": "directory"}, {"path": "linked", "uid": 101, "gid": 102, "mode": 420, "acl": "user::rw-\n", "xattrs": {"user.note": "bWV0YQ=="}, "type": "file", "size": 7, "sha256": "b" * 64, "sparse": [[0, 7]]}, {"path": "link", "uid": 101, "gid": 102, "mode": 511, "acl": "user::rwx\n", "xattrs": {}, "type": "symlink", "target": "linked"}], "hardlink_groups": [["linked", "other-link"]]}
MANIFEST_B = {"entries": [{"path": ".", "uid": 7, "gid": 8, "mode": 448, "acl": "user::rwx\n", "xattrs": {}, "type": "directory"}], "hardlink_groups": []}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _receipt(manifest=MANIFEST_A):
    identity = lambda name: {"name": name, "driver": "local", "scope": "local", "created_at": "2026-09-12T00:00:00Z", "labels": {"test.owner": "hosted-backup"}}
    return {"schema_version": 2, "helper_image": IMAGE, "archive_sha256": "a" * 64, "source_volume": identity(SOURCE), "backup_volume": identity(BACKUP), "source_manifest": manifest, "source_manifest_sha256": _digest(manifest), "backup_manifest": manifest, "backup_manifest_sha256": _digest(manifest)}


class FakeClient:
    def __init__(self, outputs=(), missing=()):
        self.outputs = list(outputs)
        self.missing = set(missing)
        self.calls = []
        self.volumes = SimpleNamespace(get=self.get)
        self.containers = SimpleNamespace(run=self.run)

    def get(self, name):
        if name in self.missing:
            raise NotFound("volume", "missing")
        return SimpleNamespace(name=name, attrs={"Name": name, "Driver": "local", "Scope": "local", "CreatedAt": "2026-09-12T00:00:00Z", "Labels": {"test.owner": "hosted-backup"}})

    def run(self, image, **kwargs):
        self.calls.append((image, kwargs))
        answer = self.outputs.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.asyncio
async def test_snapshot_returns_only_a_receipt_after_source_and_backup_metadata_agree():
    """Break caught: returning after copying without comparing the two manifests."""
    client = FakeClient(["HOSTED_BACKUP_SOURCE=" + json.dumps(MANIFEST_A) + "\nHOSTED_BACKUP_BACKUP=" + json.dumps(MANIFEST_A) + "\nHOSTED_BACKUP_ARCHIVE=" + "a" * 64 + "\n"])
    receipt = await snapshot_volume(client, volume=SOURCE, backup_volume=BACKUP, image=IMAGE)
    assert receipt["source_manifest"] == MANIFEST_A
    assert receipt["backup_manifest_sha256"] == _digest(MANIFEST_A)
    _, call = client.calls[0]
    assert call["network_disabled"] is True and call["environment"] == {} and call["remove"] is True
    assert call["entrypoint"] == ["/bin/sh", "-ec"]
    assert isinstance(call["command"], list) and len(call["command"]) == 1
    assert call["volumes"][SOURCE]["mode"] == "ro"


@pytest.mark.asyncio
async def test_snapshot_refuses_metadata_mismatch_instead_of_issuing_verified_receipt():
    """Break caught: a constant receipt or last-process-only success hides a bad copy."""
    client = FakeClient(["HOSTED_BACKUP_SOURCE=" + json.dumps(MANIFEST_A) + "\nHOSTED_BACKUP_BACKUP=" + json.dumps(MANIFEST_B) + "\n"])
    with pytest.raises(HostedBackupError, match="does not agree"):
        await snapshot_volume(client, volume=SOURCE, backup_volume=BACKUP, image=IMAGE)


@pytest.mark.asyncio
async def test_snapshot_propagates_helper_producer_exit_42():
    """Break caught: treating a failed archive producer as a successful pipeline."""
    error = ContainerError(SimpleNamespace(), 42, ["tar"], IMAGE, b"producer failed")
    client = FakeClient([error])
    with pytest.raises(HostedBackupError, match="exited 42"):
        await snapshot_volume(client, volume=SOURCE, backup_volume=BACKUP, image=IMAGE)


@pytest.mark.asyncio
async def test_restore_missing_backup_refuses_before_any_helper_can_mount_or_change_target():
    """Break caught: auto-creating or clearing a target when its backup is absent."""
    client = FakeClient(missing={BACKUP})
    with pytest.raises(HostedBackupError, match="missing"):
        await restore_volume(client, receipt=_receipt(), image=IMAGE)
    assert client.calls == []


@pytest.mark.asyncio
async def test_restore_corrupt_backup_refuses_before_destructive_target_helper():
    """Break caught: deleting target before backup-manifest verification."""
    client = FakeClient(["HOSTED_BACKUP_RESULT=" + json.dumps(MANIFEST_B) + "\n"])
    with pytest.raises(HostedBackupError, match="corrupt"):
        await restore_volume(client, receipt=_receipt(), image=IMAGE)
    assert len(client.calls) == 1
    assert SOURCE not in client.calls[0][1]["volumes"]


@pytest.mark.asyncio
async def test_restore_requires_full_metadata_manifest_after_extract():
    """Break caught: success after extract despite uid/xattr/ACL/link/sparse drift."""
    client = FakeClient(["HOSTED_BACKUP_RESULT=" + json.dumps(MANIFEST_A) + "\nHOSTED_BACKUP_ARCHIVE=" + "a" * 64 + "\n", "HOSTED_BACKUP_RESULT=" + json.dumps(MANIFEST_B) + "\n"])
    with pytest.raises(HostedBackupError, match="restored volume manifest"):
        await restore_volume(client, receipt=_receipt(), image=IMAGE)
    assert SOURCE in client.calls[1][1]["volumes"]


@pytest.mark.asyncio
async def test_archive_corruption_with_intact_payload_never_mounts_target_writable():
    receipt = _receipt()
    receipt["archive_sha256"] = "a" * 64
    client = FakeClient([
        "HOSTED_BACKUP_RESULT=" + json.dumps(MANIFEST_A) + "\nHOSTED_BACKUP_ARCHIVE=" + "b" * 64 + "\n",
        "HOSTED_BACKUP_RESULT=" + json.dumps(MANIFEST_A) + "\n",
    ])
    with pytest.raises(HostedBackupError, match="archive"):
        await restore_volume(client, receipt=receipt, image=IMAGE)
    assert len(client.calls) == 1
    assert SOURCE not in client.calls[0][1]["volumes"]
