"""Hosted liveness and destroy operations must hold the migration home fence."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal
from orchestrator.hosted_operation_lease import HostedOperationDenied, hosted_operation_lease
from orchestrator.reconcile import reconcile_liveness


def _inject_journal(monkeypatch, journal: HostedMigrationJournal) -> None:
    """Keep direct fence checks and operation leases on the same durable state."""
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)


def _record(sandbox_id: str, volume: str) -> dict:
    return {
        "schema_version": 1, "sandbox_id": sandbox_id, "phase": "admitted",
        "old_id": "old", "old_name": sandbox_id, "old_image": "sha256:" + "a" * 64,
        "source_volume": volume, "source_identity": {"name": volume},
        "row_identity": {"sandbox_id": sandbox_id}, "target_name": f"{sandbox_id}-target",
        "target_image": "sha256:" + "b" * 64, "operation_label": "test",
        "backup_name": "backup", "helper_image": "sha256:" + "c" * 64,
        "rollback_name": f"{sandbox_id}-old", "verify_timeout": 1, "stop_timeout": 1,
    }


@pytest.mark.asyncio
async def test_ec2_operation_lease_fails_closed_without_durable_journal(monkeypatch):
    """Break caught: EC2 lifecycle cannot bypass an unavailable migration journal."""
    monkeypatch.setattr("orchestrator.hosted_operation_lease.settings.host_tier", "ec2")
    with pytest.raises(HostedOperationDenied, match="lease unavailable"):
        async with hosted_operation_lease("ec2-box", "layer-ec2-box"):
            raise AssertionError("missing journal admitted an EC2 operation")


@pytest.mark.asyncio
async def test_ec2_operation_lease_allows_clean_journal_then_denies_pending_home(monkeypatch, tmp_path):
    """Break caught: a pending EC2 migration must fence its exact durable home."""
    journal = HostedMigrationJournal(tmp_path)
    _inject_journal(monkeypatch, journal)
    monkeypatch.setattr("orchestrator.hosted_operation_lease.settings.host_tier", "ec2")

    async with hosted_operation_lease("ec2-box", "layer-ec2-box"):
        pass

    journal.write(_record("old-box", "layer-ec2-box"))
    with pytest.raises(HostedOperationDenied, match="migration is pending"):
        async with hosted_operation_lease("ec2-box", "layer-ec2-box"):
            raise AssertionError("pending EC2 home admitted lifecycle work")


@pytest.mark.asyncio
async def test_liveness_excludes_only_migrating_home_while_reconciling_unrelated_home(monkeypatch, tmp_path):
    """A migration admission cannot race its row to stopped, but is not fleet-wide."""
    journal = HostedMigrationJournal(tmp_path)
    journal.write(_record("box-a", "home-a"))
    _inject_journal(monkeypatch, journal)
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")
    monkeypatch.setattr("orchestrator.reconcile._alive_container_ids", lambda *_: {"live-b"})
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: object())

    class Store:
        def __init__(self): self.called = None
        async def list(self):
            return [
                SimpleNamespace(sandbox_id="box-a", persistence_volume="home-a", tier="hosted"),
                SimpleNamespace(sandbox_id="box-b", persistence_volume="home-b", tier="hosted"),
            ]
        async def reconcile(self, alive_ids, *, tier, exclude_sandbox_ids=frozenset(), include_sandbox_ids=None):
            self.called = (alive_ids, tier, exclude_sandbox_ids, include_sandbox_ids)
            return {"stopped": [], "refreshed": 1}

    store = Store()
    assert await reconcile_liveness(store) == {"stopped": [], "refreshed": 1}
    assert store.called == ({"live-b"}, "hosted", frozenset({"box-a"}), frozenset({"box-b"}))


@pytest.mark.asyncio
async def test_liveness_derives_missing_home_and_releases_lease_on_docker_failure(monkeypatch, tmp_path):
    """A legacy null volume neither bypasses the lease nor leaks it on an early return."""
    from orchestrator.hosted_operation_lease import hosted_operation_lease
    journal = HostedMigrationJournal(tmp_path)
    _inject_journal(monkeypatch, journal)
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")
    user_id = "12345678-1234-1234-1234-123456789abc"
    class Store:
        async def list(self): return [SimpleNamespace(sandbox_id="legacy", persistence_volume=None, user_id=user_id, tier="hosted")]
        async def reconcile(self, *_args, **_kwargs): raise AssertionError("Docker failure must not mutate")
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    assert await reconcile_liveness(Store()) == {"stopped": [], "refreshed": 0}
    # The early Docker return closed the lease: migration can now acquire exclusivity.
    from orchestrator.storage_layout import user_volume_name
    with journal.lock(f"volume-{user_volume_name(user_id)}"):
        pass


@pytest.mark.asyncio
async def test_postgres_liveness_update_rechecks_snapshot_identity_and_lease_scope(monkeypatch):
    """The SQL mutation itself must reject a replaced/deleted/unleased row."""
    from orchestrator.store import PostgresSandboxStore
    calls = []
    class Conn:
        async def fetch(self, query, *args):
            calls.append((query, args))
            return [{"sandbox_id": "box-a", "container_id": "old-container"}]
        async def execute(self, query, *args):
            calls.append((query, args))
            return "UPDATE 0"  # models a concurrent replacement winning the CAS.
    class Acquire:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *_args): return False
    class Pool:
        def acquire(self): return Acquire()
    store = PostgresSandboxStore("postgresql://unused")
    async def pool(): return Pool()
    async def retry(operation): return await operation()
    monkeypatch.setattr(store, "_get_pool", pool)
    monkeypatch.setattr(store, "_execute_with_retry", retry)
    result = await store.reconcile({"different"}, tier="hosted", include_sandbox_ids=frozenset({"box-a"}))
    assert result["stopped"] == []
    update, args = calls[1]
    assert "container_id IS NOT DISTINCT FROM $2" in update
    assert "deleted_at IS NULL" in update
    assert "tier = $3" in update
    assert "sandbox_id = ANY($5::text[])" in update
    assert args == ("box-a", "old-container", "hosted", [], ["box-a"])


@pytest.mark.asyncio
async def test_destroy_refuses_pending_same_home_before_any_teardown(monkeypatch, tmp_path):
    """A reaper/direct destroy cannot mutate a home after migration has admitted it."""
    from orchestrator import sandbox_manager
    journal = HostedMigrationJournal(tmp_path)
    journal.write(_record("old", "home-a"))
    monkeypatch.setattr(sandbox_manager.settings, "host_tier", "hosted")
    _inject_journal(monkeypatch, journal)
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: SimpleNamespace(
        get=lambda _id: _one(SimpleNamespace(sandbox_id="box-new", persistence_volume="home-a", tier="hosted")),
    ))
    called = False
    async def teardown(*_args, **_kwargs):
        nonlocal called
        called = True
        return True
    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", teardown)

    with pytest.raises(HostedOperationDenied):
        await sandbox_manager.destroy_sandbox("box-new")
    assert called is False


@pytest.mark.asyncio
async def test_destroy_on_unrelated_home_proceeds_while_migration_is_pending(monkeypatch, tmp_path):
    from orchestrator import sandbox_manager
    journal = HostedMigrationJournal(tmp_path)
    journal.write(_record("old", "home-a"))
    monkeypatch.setattr(sandbox_manager.settings, "host_tier", "hosted")
    _inject_journal(monkeypatch, journal)
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: SimpleNamespace(
        get=lambda _id: _one(SimpleNamespace(sandbox_id="box-b", persistence_volume="home-b", tier="hosted")),
    ))
    async def teardown(*_args, **_kwargs):
        return True
    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", teardown)

    assert await sandbox_manager.destroy_sandbox("box-b") is True


@pytest.mark.asyncio
async def test_destroy_derives_legacy_home_and_refuses_pending_migration(monkeypatch, tmp_path):
    """A legacy null persistence_volume cannot bypass the hosted shared home fence."""
    from orchestrator import sandbox_manager
    journal = HostedMigrationJournal(tmp_path)
    user_id = "12345678-1234-1234-1234-123456789abc"
    from orchestrator.storage_layout import user_volume_name
    journal.write(_record("old", user_volume_name(user_id)))
    monkeypatch.setattr(sandbox_manager.settings, "host_tier", "hosted")
    _inject_journal(monkeypatch, journal)
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: SimpleNamespace(
        get=lambda _id: _one(SimpleNamespace(sandbox_id="legacy", persistence_volume=None, user_id=user_id, tier="hosted")),
    ))
    async def teardown(*_args, **_kwargs): raise AssertionError("unleased teardown")
    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", teardown)
    with pytest.raises(HostedOperationDenied):
        await sandbox_manager.destroy_sandbox("legacy")


async def _one(value):
    return value
