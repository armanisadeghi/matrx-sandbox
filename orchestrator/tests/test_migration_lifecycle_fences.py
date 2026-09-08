from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from orchestrator import sandbox_manager
from orchestrator.routes import sandboxes


@pytest.mark.asyncio
async def test_declined_store_transition_never_stops_container(monkeypatch):
    store = SimpleNamespace(get=AsyncMock(return_value=object()), update_status=AsyncMock(return_value=False))
    docker = Mock(side_effect=AssertionError("Docker must not be reached after refused transition"))
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", docker)
    assert await sandbox_manager.destroy_sandbox("sbx-fenced") is False
    docker.assert_not_called()


@pytest.mark.asyncio
async def test_reset_cannot_wipe_or_create_when_destroy_is_fenced(monkeypatch):
    row = SimpleNamespace(user_id="user", name="name", tier="ec2", template="slim",
                          template_version="version", labels={}, ttl_seconds=60, config={}, organization_id="org")
    monkeypatch.setattr(sandbox_manager, "get_sandbox", AsyncMock(return_value=row))
    monkeypatch.setattr(sandbox_manager, "destroy_sandbox", AsyncMock(return_value=False))
    wipe, create = AsyncMock(), AsyncMock()
    monkeypatch.setattr(sandbox_manager, "delete_user_volume", wipe)
    monkeypatch.setattr(sandbox_manager, "create_sandbox", create)
    with pytest.raises(HTTPException) as refusal:
        await sandboxes.reset_sandbox("sbx-fenced", wipe_volume=True)
    assert refusal.value.status_code == 409
    wipe.assert_not_awaited()
    create.assert_not_awaited()
