"""Connection-hook safety and coalescing for the permanent dev worker."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from orchestrator import connection_hooks
from orchestrator.models import SandboxResponse, SandboxStatus


def _sandbox() -> SandboxResponse:
    return SandboxResponse(
        sandbox_id="sbx-development",
        user_id="00000000-0000-0000-0000-000000000001",
        organization_id="22222222-2222-4222-8222-222222222222",
        status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc),
        template="development",
    )


@pytest.mark.asyncio
async def test_connection_hook_refreshes_image_then_runs_safe_sync(monkeypatch):
    connection_hooks._last_results.clear()
    connection_hooks._locks.clear()
    migrate = AsyncMock(return_value={"status": "already_current"})
    execute = AsyncMock(return_value=(0, "aidream OK\nmatrx-frontend UPDATED", "", "/home/agent"))

    monkeypatch.setattr("orchestrator.migrate.migrate_sandbox", migrate)
    monkeypatch.setattr(connection_hooks.sandbox_manager, "_get_store", lambda: object())
    monkeypatch.setattr(connection_hooks.sandbox_manager, "exec_in_sandbox", execute)

    result = await connection_hooks.prepare_development_connection(_sandbox())

    assert result["status"] == "ok"
    assert result["image_refresh"] == {"status": "already_current"}
    assert "matrx-frontend UPDATED" in result["summary"]
    assert execute.await_args.kwargs["command"] == connection_hooks._SYNC_COMMAND
    assert "reset" not in connection_hooks._SYNC_COMMAND
    assert "clean" not in connection_hooks._SYNC_COMMAND
    migrate.assert_awaited_once()


@pytest.mark.asyncio
async def test_connection_hook_keeps_binding_available_when_repo_is_dirty(monkeypatch):
    connection_hooks._last_results.clear()
    connection_hooks._locks.clear()
    monkeypatch.setattr(
        "orchestrator.migrate.migrate_sandbox",
        AsyncMock(return_value={"status": "busy_deferred"}),
    )
    monkeypatch.setattr(connection_hooks.sandbox_manager, "_get_store", lambda: object())
    monkeypatch.setattr(
        connection_hooks.sandbox_manager,
        "exec_in_sandbox",
        AsyncMock(return_value=(1, "aidream REFUSED working tree is dirty", "", "/home/agent")),
    )

    result = await connection_hooks.prepare_development_connection(_sandbox())

    assert result["status"] == "completed_with_warnings"
    assert "dirty" in result["summary"]


@pytest.mark.asyncio
async def test_connection_hook_coalesces_duplicate_binding_calls(monkeypatch):
    connection_hooks._last_results.clear()
    connection_hooks._locks.clear()
    monkeypatch.setattr(
        "orchestrator.migrate.migrate_sandbox",
        AsyncMock(return_value={"status": "already_current"}),
    )
    monkeypatch.setattr(connection_hooks.sandbox_manager, "_get_store", lambda: object())
    execute = AsyncMock(return_value=(0, "all current", "", "/home/agent"))
    monkeypatch.setattr(connection_hooks.sandbox_manager, "exec_in_sandbox", execute)

    first = await connection_hooks.prepare_development_connection(_sandbox())
    second = await connection_hooks.prepare_development_connection(_sandbox())

    assert first["cached"] is False
    assert second["cached"] is True
    execute.assert_awaited_once()
