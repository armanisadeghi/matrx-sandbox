"""EC2 reset wipe must not delete a home claimed by a concurrent lifecycle."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import multiprocessing
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError
from orchestrator.hosted_operation_lease import HostedOperationDenied
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.storage_layout import ec2_home_volume_name
from orchestrator.store import InMemorySandboxStore


USER = "00000000-0000-4000-8000-000000000001"
ORG = "22222222-2222-4222-8222-222222222222"
SID = "sbx-0123456789ab"


def _hold_lock(root: str, key: str, ready, release) -> None:
    journal = HostedMigrationJournal(Path(root))
    with journal.lock(key):
        ready.set()
        release.wait(5)


def _try_lock(root: str, key: str, queue) -> None:
    journal = HostedMigrationJournal(Path(root))
    try:
        with journal.lock(key):
            queue.put("acquired")
    except HostedMigrationStateError:
        queue.put("blocked")


def _row(*, named_home: bool) -> SandboxResponse:
    return SandboxResponse(
        sandbox_id=SID,
        user_id=USER,
        organization_id=ORG,
        tier="ec2",
        status=SandboxStatus.STOPPED,
        container_id="container-exact",
        created_at=datetime.now(timezone.utc),
        persistence_volume=ec2_home_volume_name(SID) if named_home else None,
    )


def _client(row: SandboxResponse, *, attached: bool = False, remove=None):
    labels = {
        "matrx.sandbox_id": row.sandbox_id,
        "matrx.user_id": row.user_id,
        "matrx.organization_id": row.organization_id,
        "matrx.tier": "ec2",
    }
    container = SimpleNamespace(
        id=row.container_id,
        status="exited",
        attrs={"Config": {"Labels": labels}},
        reload=MagicMock(),
        remove=remove or MagicMock(),
    )
    reference = row.persistence_volume
    volume = SimpleNamespace(
        attrs={"Name": reference, "Driver": "local", "Scope": "local", "Options": {}, "Labels": {
            "matrx.owner": "orchestrator", "matrx.sandbox_id": SID,
            "matrx.user_id": USER, "matrx.organization_id": ORG,
            "matrx.kind": "ec2-home", "matrx.tier": "ec2",
        }},
        reload=MagicMock(),
        remove=remove or MagicMock(),
    )
    return SimpleNamespace(
        containers=SimpleNamespace(
            get=MagicMock(return_value=container),
            list=MagicMock(return_value=[object()] if attached else []),
        ),
        volumes=SimpleNamespace(get=MagicMock(return_value=volume)),
        container=container,
        volume=volume,
    )


@pytest.fixture
def ec2(monkeypatch, tmp_path):
    from orchestrator import sandbox_manager

    journal = HostedMigrationJournal(tmp_path)
    store = InMemorySandboxStore()
    monkeypatch.setattr(sandbox_manager.settings, "host_tier", "ec2")
    monkeypatch.setattr(sandbox_manager, "_store", store)
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    return store, journal, sandbox_manager


@pytest.mark.asyncio
async def test_named_ec2_wipe_removes_only_a_current_terminal_unattached_home(ec2, monkeypatch):
    """Break caught: reset removes a home after its row has resumed or acquired a writer."""
    store, _journal, sandbox_manager = ec2
    row = _row(named_home=True)
    await store.save(row)
    client = _client(row)
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)

    assert await sandbox_manager.delete_ec2_home_volume(row) is True
    client.containers.list.assert_called_once_with(all=True, filters={"volume": row.persistence_volume})
    client.volume.remove.assert_called_once_with(force=False)


@pytest.mark.asyncio
async def test_named_ec2_wipe_refuses_attached_successor_before_remove(ec2, monkeypatch):
    """Break caught: a reset wipes a durable home mounted by a successor container."""
    store, _journal, sandbox_manager = ec2
    row = _row(named_home=True)
    await store.save(row)
    client = _client(row, attached=True)
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)

    with pytest.raises(RuntimeError, match="successor or writer"):
        await sandbox_manager.delete_ec2_home_volume(row)
    client.volume.remove.assert_not_called()


@pytest.mark.asyncio
async def test_ec2_wipe_refuses_when_a_concurrent_reuse_holds_real_lifecycle_lock(ec2, monkeypatch):
    """Break caught: reset checks a lock but releases it before Docker removes a reused home."""
    store, journal, sandbox_manager = ec2
    row = _row(named_home=True)
    await store.save(row)
    client = _client(row)
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_lock,
        args=(str(journal.root), f"lifecycle-{row.persistence_volume}", ready, release),
    )
    holder.start()
    assert ready.wait(3)
    try:
        with pytest.raises(HostedOperationDenied, match="lease unavailable"):
            await sandbox_manager.delete_ec2_home_volume(row)
        client.volumes.get.assert_not_called()
    finally:
        release.set()
        holder.join(5)


@pytest.mark.asyncio
async def test_legacy_layer_wipe_refuses_changed_row_before_container_remove(ec2, monkeypatch):
    """Break caught: stale reset input removes a legacy layer after resume changed its row."""
    store, _journal, sandbox_manager = ec2
    row = _row(named_home=False)
    await store.save(row)
    stale = row.model_copy(deep=True)
    row.status = SandboxStatus.READY
    await store.save(row)
    client = _client(stale)
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)

    with pytest.raises(RuntimeError, match="no longer terminal and exact"):
        await sandbox_manager.wipe_retained_ec2_layer(stale)
    client.containers.get.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_layer_wipe_removes_only_the_owned_terminal_container(ec2, monkeypatch):
    """Break caught: a layer wipe removes by a mutable name instead of its terminal recorded identity."""
    store, _journal, sandbox_manager = ec2
    row = _row(named_home=False)
    await store.save(row)
    client = _client(row)
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)

    await sandbox_manager.wipe_retained_ec2_layer(row)
    client.containers.get.assert_called_once_with("container-exact")
    client.container.remove.assert_called_once_with(force=False)


@pytest.mark.asyncio
async def test_cancellation_waits_for_ec2_home_remove_before_releasing_lease(ec2, monkeypatch):
    """Break caught: cancellation releases the lifecycle lock while Docker removal still writes."""
    store, journal, sandbox_manager = ec2
    row = _row(named_home=True)
    await store.save(row)
    started, release = threading.Event(), threading.Event()

    def slow_remove(*, force):
        assert force is False
        started.set()
        assert release.wait(3)

    client = _client(row, remove=slow_remove)
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)
    task = asyncio.create_task(sandbox_manager.delete_ec2_home_volume(row))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    queue = multiprocessing.Queue()
    contender = multiprocessing.Process(
        target=_try_lock,
        args=(str(journal.root), f"lifecycle-{row.persistence_volume}", queue),
    )
    contender.start()
    contender.join(5)
    assert queue.get(timeout=1) == "blocked"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
