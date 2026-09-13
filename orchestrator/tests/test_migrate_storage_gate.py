"""Regression for real slim lifecycle: entrypoint-slim never hot-syncs S3."""
from types import SimpleNamespace

import pytest

from orchestrator import migrate


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["slim", "bare"])
async def test_noncore_without_shared_home_never_enters_s3_migration(monkeypatch, template):
    old = SimpleNamespace(
        labels={"matrx.template": template, "matrx.tier": "ec2"},
        attrs={"Image": "old", "Config": {"Cmd": ["/opt/sandbox/scripts/entrypoint-slim.sh"]}, "HostConfig": {}},
    )
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda _: old))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: client)
    monkeypatch.setattr(migrate, "current_image", lambda *_: SimpleNamespace(tag="new", image_id="new"))
    from tests.conftest import seed_sandbox_knobs

    seed_sandbox_knobs({"enable_s3_migrate": True})
    async def forbidden(*args, **kwargs):
        pytest.fail("Noncore entered S3 migration without a verified hot-sync lifecycle")
    monkeypatch.setattr(migrate, "_migrate_s3_ordered", forbidden)
    result = await migrate.migrate_sandbox("storage-contract", store=None)
    assert result["status"] == "unsupported_storage"
    assert "git" in result["reason"]


@pytest.mark.asyncio
async def test_correlated_core_s3_migration_refuses_without_durable_status(monkeypatch):
    """A lost HTTP response must not make an S3 cutover outcome unknowable."""
    old = SimpleNamespace(
        labels={"matrx.template": "core", "matrx.tier": "ec2"},
        attrs={
            "Image": "old",
            "Config": {
                "Cmd": ["/opt/sandbox/scripts/entrypoint.sh"],
                "Env": [],
            },
            "HostConfig": {"Binds": []},
        },
    )
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda _: old))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: client)
    monkeypatch.setattr(migrate.settings, "host_tier", "ec2")
    monkeypatch.setattr(
        migrate,
        "current_image",
        lambda *_: SimpleNamespace(tag="new", image_id="new", version="v2"),
    )
    from tests.conftest import seed_sandbox_knobs

    seed_sandbox_knobs({"enable_s3_migrate": True})
    async def s3_path(*_args, **_kwargs):
        pytest.fail("S3 migration ran without a durable exact-operation journal")

    monkeypatch.setattr(migrate, "_migrate_s3_ordered", s3_path)
    result = await migrate.migrate_sandbox(
        "ec2-contract",
        store=None,
        interrupt_attached_sessions=True,
    )

    assert result["status"] == "unsupported_storage"
    assert "durable exact-operation journal" in result["reason"]
