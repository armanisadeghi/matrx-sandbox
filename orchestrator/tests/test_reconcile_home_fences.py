"""Discovery and zombie reaping hold real hosted-home leases through mutation."""
from __future__ import annotations

import multiprocessing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.reconcile import reap_zombie_containers, reconcile_from_docker
from orchestrator.store import InMemorySandboxStore

USER = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"


def _record(sandbox_id: str, volume: str, *, phase: str = "admitted", target_id: str | None = None) -> dict:
    record = {
        "schema_version": 1, "sandbox_id": sandbox_id, "phase": phase,
        "old_id": "retained-old", "old_name": sandbox_id, "old_image": "sha256:" + "a" * 64,
        "source_volume": volume, "source_identity": {"name": volume},
        "row_identity": {"sandbox_id": sandbox_id}, "target_name": f"{sandbox_id}-target",
        "target_image": "sha256:" + "b" * 64, "target_id": target_id,
        "operation_label": "test-op", "backup_name": "backup", "helper_image": "sha256:" + "c" * 64,
        "rollback_name": f"{sandbox_id}-rollback", "verify_timeout": 1, "stop_timeout": 1,
    }
    if phase == "committed":
        record["backup_receipt"] = {"verified": True}
    return record


def _exclusive_try(root: str, key: str, queue) -> None:
    journal = HostedMigrationJournal(Path(root))
    try:
        with journal.lock(key):
            queue.put("acquired")
    except HostedMigrationStateError:
        queue.put("blocked")


def _exclusive_result(journal: HostedMigrationJournal, volume: str) -> str:
    queue = multiprocessing.Queue()
    process = multiprocessing.Process(
        target=_exclusive_try, args=(str(journal.root), f"volume-{volume}", queue),
    )
    process.start()
    process.join(5)
    try:
        assert process.exitcode == 0
        return queue.get(timeout=1)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        queue.close()
        queue.join_thread()


def _patch_journal(monkeypatch, journal: HostedMigrationJournal) -> None:
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")


def _container(container_id: str, sandbox_id: str, volume: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=container_id,
        attrs={
            "Config": {"Labels": {
                "matrx.sandbox_id": sandbox_id, "matrx.user_id": USER,
                "matrx.organization_id": ORG, "matrx.tier": "hosted",
            }},
            "State": {"Running": True, "Status": "running"},
            "Mounts": [{"Type": "volume", "Name": volume, "Destination": "/home/agent"}],
            "NetworkSettings": {"Ports": {}},
        },
        reload=lambda: None,
    )


def _client(containers) -> SimpleNamespace:
    return SimpleNamespace(containers=SimpleNamespace(list=lambda **_: containers))


@pytest.mark.asyncio
async def test_discovery_holds_home_lease_through_real_store_save_and_releases_it(monkeypatch, tmp_path):
    """Break caught: discovery releases the shared home lock before its store write finishes."""
    journal = HostedMigrationJournal(tmp_path)
    _patch_journal(monkeypatch, journal)
    volume = "home-discovery"
    observed = []

    class ObservingStore(InMemorySandboxStore):
        async def save(self, sandbox):
            observed.append(_exclusive_result(journal, volume))
            await super().save(sandbox)

    container = _container("runtime-discovery", "sbx-discovery", volume)
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: _client([container]))
    store = ObservingStore()

    summary = await reconcile_from_docker(store)

    assert summary["reconciled"] == 1
    assert observed == ["blocked"]
    assert (await store.get("sbx-discovery")).container_id == "runtime-discovery"
    assert _exclusive_result(journal, volume) == "acquired"


@pytest.mark.asyncio
async def test_zombie_reap_holds_home_lease_through_remove_and_releases_it(monkeypatch, tmp_path):
    """Break caught: zombie removal runs after its discovery home lease has exited."""
    journal = HostedMigrationJournal(tmp_path)
    _patch_journal(monkeypatch, journal)
    volume = "home-zombie"
    container = _container("runtime-zombie", "sbx-zombie", volume)
    observed = []
    container.remove = lambda **kwargs: observed.append((kwargs, _exclusive_result(journal, volume)))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: _client([container]))
    store = InMemorySandboxStore()
    await store.save(SandboxResponse(
        sandbox_id="sbx-zombie", user_id=USER, organization_id=ORG,
        status=SandboxStatus.STOPPED, container_id="runtime-zombie",
        created_at=datetime.now(timezone.utc),
        tier="hosted", persistence_volume=volume,
    ))

    assert await reap_zombie_containers(store) == ["sbx-zombie"]
    assert observed == [({"force": True}, "blocked")]
    assert _exclusive_result(journal, volume) == "acquired"


@pytest.mark.asyncio
async def test_pending_sibling_home_blocks_discovery_and_zombie_removal(monkeypatch, tmp_path):
    """Break caught: fences check only sandbox ID and let a sibling mutate its migrating home."""
    journal = HostedMigrationJournal(tmp_path)
    volume = "home-pending"
    journal.write(_record("sbx-migrating", volume))
    _patch_journal(monkeypatch, journal)
    container = _container("sibling-runtime", "sbx-sibling", volume)
    removed = []
    container.remove = lambda **kwargs: removed.append(kwargs)
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: _client([container]))
    store = InMemorySandboxStore()
    await store.save(SandboxResponse(
        sandbox_id="sbx-sibling", user_id=USER, organization_id=ORG,
        status=SandboxStatus.STOPPED, container_id="sibling-runtime",
        created_at=datetime.now(timezone.utc),
        tier="hosted", persistence_volume=volume,
    ))

    assert (await reconcile_from_docker(store))["failed"] == 1
    assert await reap_zombie_containers(store) == []
    assert removed == []


@pytest.mark.asyncio
async def test_pending_home_does_not_block_unrelated_discovery(monkeypatch, tmp_path):
    """Break caught: one pending migration globally stops hosted discovery."""
    journal = HostedMigrationJournal(tmp_path)
    journal.write(_record("sbx-migrating", "home-a"))
    _patch_journal(monkeypatch, journal)
    container = _container("runtime-other", "sbx-other", "home-b")
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: _client([container]))
    store = InMemorySandboxStore()

    assert (await reconcile_from_docker(store))["reconciled"] == 1
    assert (await store.get("sbx-other")).persistence_volume == "home-b"
