"""Boot reconciliation must never erase durable sandbox metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.reconcile import reconcile_from_docker


def _patch_journal(monkeypatch, journal):
    """Both runtime lookups must use the same real temporary journal."""
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)


@pytest.mark.asyncio
async def test_existing_row_metadata_survives_docker_reconcile(monkeypatch, tmp_path):
    from orchestrator.hosted_migration import HostedMigrationJournal
    _patch_journal(monkeypatch, HostedMigrationJournal(tmp_path))
    sandbox_id = "sbx-preserve-meta"
    user_id = "11111111-1111-4111-8111-111111111111"
    organization_id = "22222222-2222-4222-8222-222222222222"
    created_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
    existing = SandboxResponse(
        sandbox_id=sandbox_id,
        user_id=user_id,
        organization_id=organization_id,
        status=SandboxStatus.RUNNING,
        container_id="old-container",
        created_at=created_at,
        hot_path="/custom/hot",
        cold_path="/custom/cold",
        config={"env": {"USER_SETTING": "preserve"}, "nested": {"enabled": True}},
        ttl_seconds=9876,
        tier="hosted",
        template="slim",
        template_version="old-version",
        labels={"customer-label": "preserve"},
        persistence_volume="matrx-user-existing",
    )

    attrs = {
        "Config": {
            "Labels": {
                "matrx.sandbox_id": sandbox_id,
                "matrx.user_id": user_id,
                "matrx.tier": "hosted",
                "matrx.template": "slim",
                "matrx.template_version": "current-version",
            },
        },
        "State": {"Running": True, "Status": "running"},
        "Mounts": [],
        "NetworkSettings": {"Ports": {}},
    }
    container = SimpleNamespace(id="old-container", attrs=attrs, reload=lambda: None)
    client = SimpleNamespace(
        containers=SimpleNamespace(list=lambda **_kwargs: [container]),
    )
    monkeypatch.setattr(
        "orchestrator.sandbox_manager._get_docker_client", lambda: client,
    )
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")

    class Store:
        saved = None

        async def get(self, _sandbox_id):
            return existing

        async def get_lifecycle(self, _sandbox_id):
            return None

        async def save(self, sandbox):
            self.saved = sandbox

    store = Store()
    summary = await reconcile_from_docker(store)

    assert summary["reconciled"] == 1
    assert store.saved is not None
    assert store.saved.container_id == "old-container"
    assert store.saved.organization_id == organization_id
    assert store.saved.template_version == "current-version"
    assert store.saved.created_at == created_at
    assert store.saved.hot_path == "/custom/hot"
    assert store.saved.cold_path == "/custom/cold"
    assert store.saved.config == existing.config
    assert store.saved.ttl_seconds == 9876
    assert store.saved.labels == {"customer-label": "preserve"}
    assert store.saved.persistence_volume == "matrx-user-existing"


@pytest.mark.asyncio
async def test_discovery_never_overwrites_newer_container_routing(monkeypatch, tmp_path):
    """Break caught: reconcile saves Docker's old discovery over a newer routing CAS."""
    from orchestrator.hosted_migration import HostedMigrationJournal
    from orchestrator.store import InMemorySandboxStore

    _patch_journal(monkeypatch, HostedMigrationJournal(tmp_path))
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")
    sandbox_id = "sbx-cas-fence"
    user_id = "11111111-1111-4111-8111-111111111111"
    organization_id = "22222222-2222-4222-8222-222222222222"

    class TrackingStore(InMemorySandboxStore):
        def __init__(self):
            super().__init__()
            self.discovery_writes = []

        async def save(self, sandbox):
            self.discovery_writes.append(sandbox)
            await super().save(sandbox)

    store = TrackingStore()
    existing = SandboxResponse(
        sandbox_id=sandbox_id, user_id=user_id, organization_id=organization_id,
        status=SandboxStatus.RUNNING, container_id="newer-current-runtime",
        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc), tier="hosted",
        persistence_volume="matrx-user-cas-fence",
    )
    await InMemorySandboxStore.save(store, existing)
    container = SimpleNamespace(
        id="stale-discovery-runtime",
        attrs={
            "Config": {"Labels": {
                "matrx.sandbox_id": sandbox_id, "matrx.user_id": user_id,
                "matrx.organization_id": organization_id, "matrx.tier": "hosted",
            }},
            "State": {"Running": True},
            "Mounts": [{"Type": "volume", "Name": "matrx-user-cas-fence", "Destination": "/home/agent"}],
        },
        reload=lambda: None,
    )
    monkeypatch.setattr(
        "orchestrator.sandbox_manager._get_docker_client",
        lambda: SimpleNamespace(containers=SimpleNamespace(list=lambda **_: [container])),
    )

    summary = await reconcile_from_docker(store)

    assert summary["failed"] == 1
    assert store.discovery_writes == []
    assert (await store.get(sandbox_id)).container_id == "newer-current-runtime"
