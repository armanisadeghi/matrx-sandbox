"""Regression for real slim lifecycle: entrypoint-slim never hot-syncs S3."""
from types import SimpleNamespace

import pytest

from orchestrator import migrate


@pytest.mark.asyncio
async def test_slim_without_shared_home_never_enters_s3_migration(monkeypatch):
    old = SimpleNamespace(
        labels={"matrx.template": "slim", "matrx.tier": "ec2"},
        attrs={"Image": "old", "Config": {"Cmd": ["/opt/sandbox/scripts/entrypoint-slim.sh"]}, "HostConfig": {}},
    )
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda _: old))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: client)
    monkeypatch.setattr(migrate, "current_image", lambda *_: SimpleNamespace(tag="new", image_id="new"))
    monkeypatch.setattr(migrate.settings, "enable_s3_migrate", True)
    async def forbidden(*args, **kwargs):
        pytest.fail("Slim entered S3 migration despite having no hot-sync lifecycle")
    monkeypatch.setattr(migrate, "_migrate_s3_ordered", forbidden)
    result = await migrate.migrate_sandbox("slim-storage-contract", store=None)
    assert result["status"] == "unsupported_storage"
    assert "git" in result["reason"]
