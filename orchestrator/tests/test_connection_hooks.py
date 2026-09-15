"""Connection-hook safety and coalescing for the permanent dev worker."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from orchestrator import connection_hooks, sdk_refresh
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.routes import sandboxes


@pytest.fixture(autouse=True)
def _stub_sdk_refresh(monkeypatch):
    """The SDK refresh is its own hook with its own guards
    (tests/test_sdk_refresh.py); here it is stubbed so these tests stay about
    the repository sync. It runs for EVERY template, so it cannot be skipped."""
    monkeypatch.setattr(
        sdk_refresh,
        "refresh_sdk_if_stale",
        AsyncMock(return_value={"hook": "session_start.sdk_refresh", "status": "current",
                                "from": "v1", "to": "v1"}),
    )


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
async def test_connection_hook_never_migrates_image_and_preserves_safe_sync(monkeypatch):
    connection_hooks._last_results.clear()
    connection_hooks._locks.clear()
    migrate = AsyncMock(return_value={"status": "already_current"})
    execute = AsyncMock(return_value=(0, "aidream OK\nmatrx-frontend UPDATED", "", "/home/agent"))

    monkeypatch.setattr("orchestrator.migrate.migrate_sandbox", migrate)
    monkeypatch.setattr(connection_hooks.sandbox_manager, "_get_store", lambda: object())
    monkeypatch.setattr(connection_hooks.sandbox_manager, "exec_in_sandbox", execute)

    result = await connection_hooks.prepare_development_connection(_sandbox())

    assert result["status"] == "ok"
    assert result["image_refresh"] is None
    assert "matrx-frontend UPDATED" in result["summary"]
    assert execute.await_args.kwargs["command"] == connection_hooks._SYNC_COMMAND
    assert "reset" not in connection_hooks._SYNC_COMMAND
    assert "clean" not in connection_hooks._SYNC_COMMAND
    migrate.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_prepare_connection_isolates_hook_failure_from_token_mint(
    monkeypatch, caplog
):
    hook = AsyncMock(side_effect=RuntimeError("sync transport unavailable"))
    monkeypatch.setattr(connection_hooks, "prepare_development_connection", hook)

    result = await sandboxes._prepare_connection(_sandbox())

    assert result == {
        "sdk_refresh": {"hook": "session_start.sdk_refresh", "status": "current",
                        "from": "v1", "to": "v1"},
        "status": "failed",
        "summary": "Development connection preparation failed; it will retry on the next binding.",
    }
    assert "issuing the token without hooks" in caplog.text
    assert "sync transport unavailable" in caplog.text


@pytest.mark.asyncio
async def test_prepare_connection_skips_the_repo_sync_for_an_ordinary_sandbox(monkeypatch):
    """The repository sync is the dev worker's alone — but the SDK refresh is
    every box's, so an ordinary sandbox still gets a hook report."""
    hook = AsyncMock()
    monkeypatch.setattr(connection_hooks, "prepare_development_connection", hook)
    ordinary = _sandbox().model_copy(update={"template": "default"})

    result = await sandboxes._prepare_connection(ordinary)

    assert result == {
        "sdk_refresh": {"hook": "session_start.sdk_refresh", "status": "current",
                        "from": "v1", "to": "v1"},
    }
    hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_connection_returns_only_json_safe_bounded_diagnostics(monkeypatch):
    hook = AsyncMock(
        return_value={
            "hook": "session_start.repo_sync",
            "status": "ok",
            "exit_code": 0,
            "summary": "sync complete",
            "cached": False,
            "image_refresh": {
                "status": "already_current",
                "sandbox_id": "sbx-development",
                "version": "abc123",
                "internal_client": object(),
            },
            "internal_client": object(),
        }
    )
    monkeypatch.setattr(connection_hooks, "prepare_development_connection", hook)

    result = await sandboxes._prepare_connection(_sandbox())

    assert result == {
        "sdk_refresh": {"hook": "session_start.sdk_refresh", "status": "current",
                        "from": "v1", "to": "v1"},
        "hook": "session_start.repo_sync",
        "status": "ok",
        "exit_code": 0,
        "summary": "sync complete",
        "cached": False,
        "image_refresh": {
            "status": "already_current",
            "sandbox_id": "sbx-development",
            "version": "abc123",
        },
    }


def test_persisted_sandbox_tier_is_a_plain_string():
    sandbox = _sandbox().model_copy(update={"tier": "hosted"})

    assert sandbox.tier == "hosted"
    assert sandboxes.settings.resolve_host_tier(sandbox.tier) == "hosted"
