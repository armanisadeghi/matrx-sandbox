"""Boot reconciliation must never erase durable sandbox metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.reconcile import reconcile_from_docker


@pytest.mark.asyncio
async def test_existing_row_metadata_survives_docker_reconcile(monkeypatch):
    sandbox_id = "sbx-preserve-meta"
    user_id = "11111111-1111-4111-8111-111111111111"
    created_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
    existing = SandboxResponse(
        sandbox_id=sandbox_id,
        user_id=user_id,
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
    container = SimpleNamespace(id="new-container", attrs=attrs, reload=lambda: None)
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
    assert store.saved.container_id == "new-container"
    assert store.saved.template_version == "current-version"
    assert store.saved.created_at == created_at
    assert store.saved.hot_path == "/custom/hot"
    assert store.saved.cold_path == "/custom/cold"
    assert store.saved.config == existing.config
    assert store.saved.ttl_seconds == 9876
    assert store.saved.labels == {"customer-label": "preserve"}
    assert store.saved.persistence_volume == "matrx-user-existing"
