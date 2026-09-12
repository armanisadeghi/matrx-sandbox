"""Deletion must share the durable hosted-home lease with migration/create."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal

USER = "00000000-0000-4000-8000-000000000001"


def _record(volume: str) -> dict:
    return {"schema_version": 1, "sandbox_id": "sbx-old", "phase": "admitted", "old_id": "old", "old_name": "sbx-old", "old_image": "sha256:" + "a" * 64, "source_volume": volume, "source_identity": {"name": volume}, "row_identity": {"sandbox_id": "sbx-old"}, "target_name": "sbx-old-mig", "target_image": "sha256:" + "b" * 64, "operation_label": "op", "backup_name": "backup", "helper_image": "sha256:" + "c" * 64, "rollback_name": "sbx-old-old", "verify_timeout": 1, "stop_timeout": 1}


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    from orchestrator import sandbox_manager
    journal = HostedMigrationJournal(Path(tmp_path))
    monkeypatch.setattr(sandbox_manager.settings, "host_tier", "hosted")
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    return journal


def _client(*, in_use=False, missing=False):
    volume = SimpleNamespace(remove=MagicMock())
    containers = SimpleNamespace(list=MagicMock(return_value=[object()] if in_use else []))
    volumes = SimpleNamespace(
        get=MagicMock(
            side_effect=__import__("docker.errors", fromlist=["NotFound"]).NotFound("missing")
            if missing else None,
            return_value=None if missing else volume,
        )
    )
    return SimpleNamespace(
        containers=containers,
        volumes=volumes,
        volume=volume,
    )


@pytest.mark.asyncio
async def test_pending_migration_denies_volume_delete_before_docker(hosted, monkeypatch):
    from orchestrator import sandbox_manager
    from orchestrator.storage_layout import user_volume_name
    volume = user_volume_name(USER); hosted.write(_record(volume))
    client = _client(); monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)
    with pytest.raises(RuntimeError, match="migration is pending"):
        await sandbox_manager.delete_user_volume(USER)
    client.containers.list.assert_not_called()


@pytest.mark.asyncio
async def test_held_exclusive_home_lock_denies_delete_before_docker(hosted, monkeypatch):
    """Break caught: delete checks migration once, then races an admitted migration."""
    from orchestrator import sandbox_manager
    from orchestrator.storage_layout import user_volume_name
    volume = user_volume_name(USER)
    client = _client(); monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)
    with hosted.lock(f"volume-{volume}"):
        with pytest.raises(RuntimeError, match="lease unavailable"):
            await sandbox_manager.delete_user_volume(USER)
    client.containers.list.assert_not_called()


@pytest.mark.asyncio
async def test_delete_refuses_attached_volume(hosted, monkeypatch):
    from orchestrator import sandbox_manager
    client = _client(in_use=True); monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)
    with pytest.raises(RuntimeError, match="still in use"):
        await sandbox_manager.delete_user_volume(USER)
    client.volume.remove.assert_not_called()


@pytest.mark.asyncio
async def test_delete_unused_and_missing_volume_are_safe(hosted, monkeypatch):
    from orchestrator import sandbox_manager
    client = _client(); monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)
    assert await sandbox_manager.delete_user_volume(USER) is True
    client.volume.remove.assert_called_once_with(force=False)
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: _client(missing=True))
    assert await sandbox_manager.delete_user_volume(USER) is True
