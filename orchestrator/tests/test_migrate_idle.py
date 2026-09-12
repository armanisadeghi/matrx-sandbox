"""Unit tests for the migration idle gate (protect recently-active sessions)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from orchestrator.migrate import _has_recent_heartbeat
from orchestrator.migrate import _refresh_platform_environment


class _Row:
    def __init__(self, hb):
        self.last_heartbeat_at = hb


def test_no_row_is_not_recent():
    assert _has_recent_heartbeat(None, 120) is False


def test_missing_heartbeat_is_not_recent():
    assert _has_recent_heartbeat(_Row(None), 120) is False


def test_fresh_heartbeat_is_recent():
    hb = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert _has_recent_heartbeat(_Row(hb), 120) is True


def test_old_heartbeat_is_not_recent():
    hb = datetime.now(timezone.utc) - timedelta(seconds=120 + 60)
    assert _has_recent_heartbeat(_Row(hb), 120) is False


def test_naive_heartbeat_treated_as_utc():
    # A naive timestamp must not raise; treat it as UTC.
    hb = datetime.utcnow() - timedelta(seconds=5)
    assert _has_recent_heartbeat(_Row(hb), 120) is True


def test_platform_environment_refresh_preserves_user_values(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.sandbox_manager._resolve_passthrough_keys",
        lambda: ["SUPABASE_MATRIX_HOST", "SUPABASE_MATRIX_PASSWORD"],
    )
    monkeypatch.setenv("SUPABASE_MATRIX_HOST", "east.example")
    monkeypatch.setenv("SUPABASE_MATRIX_PASSWORD", "new-secret")

    refreshed, changed = _refresh_platform_environment([
        "SUPABASE_MATRIX_HOST=west.example",
        "SUPABASE_MATRIX_PASSWORD=old-secret",
        "USER_CHOSEN_VALUE=keep-me",
    ])

    assert "USER_CHOSEN_VALUE=keep-me" in refreshed
    assert "SUPABASE_MATRIX_HOST=east.example" in refreshed
    assert "SUPABASE_MATRIX_PASSWORD=new-secret" in refreshed
    assert "SUPABASE_MATRIX_HOST=west.example" not in refreshed
    assert changed == 2


def test_platform_environment_refresh_removes_retired_platform_key(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.sandbox_manager._resolve_passthrough_keys",
        lambda: ["RETIRED_PLATFORM_KEY"],
    )
    monkeypatch.delenv("RETIRED_PLATFORM_KEY", raising=False)

    refreshed, changed = _refresh_platform_environment([
        "RETIRED_PLATFORM_KEY=stale",
        "USER_CHOSEN_VALUE=keep-me",
    ])

    assert refreshed == ["USER_CHOSEN_VALUE=keep-me"]
    assert changed == 1


# ── Widened idle gate (2026-07-09): open sessions + recent tool activity ──────
# An agent between commands and a human with an open terminal both show ZERO
# in-flight calls — the gate must still treat them as busy. The gate now runs
# BEFORE any docker lookups, so these tests need no docker.

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from orchestrator import activity


@pytest.mark.asyncio
async def test_open_session_defers_migration():
    from orchestrator.migrate import migrate_sandbox
    sid = "sbx-gate-pty00001"
    activity.session_opened(sid)
    try:
        result = await migrate_sandbox(sid, store=None, require_idle=True)
        assert result["status"] == "busy_deferred"
        assert "session" in result["reason"]
    finally:
        activity.session_closed(sid)


@pytest.mark.asyncio
async def test_confirmed_manual_migration_fences_new_work_and_allows_attached_session(
    monkeypatch,
):
    """The Code page can update its box without making its own PTY an impossible gate."""
    from orchestrator import migrate

    sid = "sbx-confirmed-pty"
    old = SimpleNamespace(
        id="old-container",
        labels={"matrx.template": "bare"},
        attrs={
            "Image": "sha256:" + "a" * 64,
            "Config": {"Env": []},
            "HostConfig": {"Binds": ["home-volume:/home/agent:rw"]},
        },
    )
    client = SimpleNamespace(
        containers=SimpleNamespace(get=lambda received: old if received == sid else None)
    )
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: client)
    monkeypatch.setattr(
        migrate,
        "current_image",
        lambda *_: SimpleNamespace(
            tag="matrx-sandbox:bare",
            image_id="sha256:" + "b" * 64,
            version="new-version",
        ),
    )
    hosted = AsyncMock(
        return_value={"status": "migrated", "sandbox_id": sid}
    )
    monkeypatch.setattr(migrate, "_migrate_hosted_ordered", hosted)

    operation_lease = await activity.acquire_operation_lease(sid)
    assert operation_lease is not None
    session = activity.session_opened(sid, operation_lease)

    async def attached_proxy():
        await session.interrupt.wait()
        activity.session_closed(sid, session)
        await activity.release_operation_lease(operation_lease)

    proxy = asyncio.create_task(attached_proxy())
    try:
        result = await migrate.migrate_sandbox(
            sid,
            store=object(),
            require_idle=True,
            interrupt_attached_sessions=True,
        )
    finally:
        await proxy

    assert result == {"status": "migrated", "sandbox_id": sid}
    assert hosted.await_args.kwargs["interrupt_attached_sessions"] is True
    assert activity.is_migrating(sid) is False


@pytest.mark.asyncio
async def test_confirmed_manual_migration_waits_for_attached_proxy_lease_release(
    monkeypatch,
):
    """The hosted engine must not run while its PTY/watch proxy is still attached."""
    from orchestrator import migrate

    sid = "sbx-interrupt-before-exclusive"
    operation_lease = await activity.acquire_operation_lease(sid)
    assert operation_lease is not None
    session = activity.session_opened(sid, operation_lease)
    released = asyncio.Event()

    async def attached_proxy():
        await session.interrupt.wait()
        await asyncio.sleep(0)
        activity.session_closed(sid, session)
        await activity.release_operation_lease(operation_lease)
        released.set()

    async def hosted_engine(_sandbox_id, **_kwargs):
        assert released.is_set()
        assert activity.open_session_count(sid) == 0
        return {"status": "migrated", "sandbox_id": sid}

    monkeypatch.setattr(migrate, "_migrate_hosted_ordered", hosted_engine)
    proxy = asyncio.create_task(attached_proxy())
    result = await migrate._migrate_hosted_with_admission(
        sid,
        interrupt_attached_sessions=True,
    )
    await proxy

    assert result == {"status": "migrated", "sandbox_id": sid}
    assert activity.is_migrating(sid) is False


@pytest.mark.asyncio
async def test_default_migration_refuses_attached_operation_lease(monkeypatch):
    """Automatic migration never interrupts a user's attached session."""
    from orchestrator import migrate

    sid = "sbx-default-keeps-session"
    operation_lease = await activity.acquire_operation_lease(sid)
    assert operation_lease is not None
    session = activity.session_opened(sid, operation_lease)
    engine = AsyncMock()
    monkeypatch.setattr(migrate, "_migrate_hosted_ordered", engine)
    try:
        result = await migrate._migrate_hosted_with_admission(sid)
        assert result["status"] == "busy_deferred"
        assert session.interrupt.is_set() is False
        engine.assert_not_awaited()
    finally:
        activity.session_closed(sid, session)
        await activity.release_operation_lease(operation_lease)


@pytest.mark.asyncio
async def test_migration_drains_pre_fence_non_session_operation(monkeypatch):
    """A request between middleware admission and route tracking must drain."""
    from orchestrator import migrate

    sid = "sbx-drain-short-operation"
    operation_lease = await activity.acquire_operation_lease(sid)
    assert operation_lease is not None
    engine = AsyncMock(return_value={"status": "migrated", "sandbox_id": sid})
    monkeypatch.setattr(migrate, "_migrate_hosted_ordered", engine)

    migration = asyncio.create_task(migrate._migrate_hosted_with_admission(sid))
    await asyncio.sleep(0)
    assert migration.done() is False
    engine.assert_not_awaited()

    await activity.release_operation_lease(operation_lease)
    assert await migration == {"status": "migrated", "sandbox_id": sid}
    engine.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmed_migration_interrupts_late_session_registration(monkeypatch):
    """A pre-fence WebSocket token may become a session after drain starts."""
    from orchestrator import migrate

    sid = "sbx-late-session-registration"
    operation_lease = await activity.acquire_operation_lease(sid)
    assert operation_lease is not None
    engine = AsyncMock(return_value={"status": "migrated", "sandbox_id": sid})
    monkeypatch.setattr(migrate, "_migrate_hosted_ordered", engine)

    migration = asyncio.create_task(
        migrate._migrate_hosted_with_admission(
            sid,
            interrupt_attached_sessions=True,
        )
    )
    await asyncio.wait_for(operation_lease.interrupt.wait(), timeout=0.5)
    session = activity.session_opened(sid, operation_lease)
    assert session.interrupt.is_set() is True
    activity.session_closed(sid, session)
    await activity.release_operation_lease(operation_lease)

    assert await migration == {"status": "migrated", "sandbox_id": sid}
    engine.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_migration_cannot_release_active_owners_fence(monkeypatch):
    """A rejected second request must not reopen tool admission under the first."""
    import asyncio

    from orchestrator import migrate

    sid = "sbx-exclusive-migration"
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def held_migration(_sandbox_id, **_kwargs):
        entered.set()
        await finish.wait()
        return {"status": "migrated", "sandbox_id": sid}

    monkeypatch.setattr(migrate, "_migrate_hosted_ordered", held_migration)
    first = asyncio.create_task(migrate._migrate_hosted_with_admission(sid))
    await entered.wait()

    second = await migrate._migrate_hosted_with_admission(sid)
    assert second == {
        "status": "busy_deferred",
        "sandbox_id": sid,
        "reason": "another migration already owns this sandbox; retry later",
    }
    assert activity.is_migrating(sid) is True

    finish.set()
    assert await first == {"status": "migrated", "sandbox_id": sid}
    assert activity.is_migrating(sid) is False


@pytest.mark.asyncio
async def test_recent_tool_activity_defers_migration():
    from orchestrator.migrate import migrate_sandbox
    sid = "sbx-gate-recent01"
    activity.note_activity(sid)  # a tool call just finished
    result = await migrate_sandbox(sid, store=None, require_idle=True)
    assert result["status"] == "busy_deferred"
    assert "activity" in result["reason"]


def test_session_refcount_balances():
    sid = "sbx-gate-refcnt01"
    assert activity.open_session_count(sid) == 0
    activity.session_opened(sid)
    activity.session_opened(sid)
    assert activity.open_session_count(sid) == 2
    activity.session_closed(sid)
    assert activity.open_session_count(sid) == 1
    activity.session_closed(sid)
    assert activity.open_session_count(sid) == 0
    activity.session_closed(sid)  # over-close must not go negative
    assert activity.open_session_count(sid) == 0
