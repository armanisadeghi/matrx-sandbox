"""Unit tests for the migration idle gate (protect recently-active sessions)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from orchestrator.migrate import _has_recent_heartbeat
from orchestrator.migrate import _refresh_platform_environment


class _Row:
    def __init__(self, hb):
        self.last_heartbeat_at = hb
        self.user_id = "22222222-2222-2222-2222-222222222222"
        self.tier = "hosted"


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

    # The aidream template with the operator knob ON — the only combination
    # that forwards a master credential (test_platform_env_isolation.py
    # covers the default-deny and the non-aidream templates).
    refreshed, changed = _refresh_platform_environment([
        "SUPABASE_MATRIX_HOST=west.example",
        "SUPABASE_MATRIX_PASSWORD=old-secret",
        "USER_CHOSEN_VALUE=keep-me",
    ], "aidream", allow_master_credentials=True)

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
    ], "aidream", allow_master_credentials=False)

    assert refreshed == ["USER_CHOSEN_VALUE=keep-me"]
    assert changed == 1


# ── Widened idle gate (2026-07-09): open sessions + recent tool activity ──────
# An agent between commands and a human with an open terminal both show ZERO
# in-flight calls — the gate must still treat them as busy. The gate now runs
# BEFORE any docker lookups, so these tests need no docker.

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from orchestrator import activity


_DEFAULT_ROW = object()


class _IdleStore:
    def __init__(self, row=_DEFAULT_ROW):
        self.row = _Row(None) if row is _DEFAULT_ROW else row

    async def get(self, _sandbox_id):
        return self.row


class _UnavailableIdleStore:
    async def get(self, _sandbox_id):
        raise RuntimeError("store unavailable")


def _migratable_old(image: str):
    return SimpleNamespace(
        id="old-container",
        labels={"matrx.template": "bare"},
        attrs={
            "Image": image,
            "Config": {"Env": []},
            "HostConfig": {"Binds": ["home-volume:/home/agent:rw"]},
            "Mounts": [{
                "Type": "volume", "Name": "home-volume",
                "Destination": "/home/agent", "RW": True,
            }],
        },
    )


def _wire_idle_admission(monkeypatch, sid, *, now=111.0, quiet_window=10):
    """Use the public migration entrypoint with real admission, not a stub."""
    from orchestrator import migrate

    old_image = "sha256:" + "a" * 64
    old = _migratable_old(old_image)
    client = SimpleNamespace(
        containers=SimpleNamespace(get=lambda received: old if received == sid else None)
    )
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: client)
    monkeypatch.setattr(migrate.settings, "host_tier", "hosted")
    # Presence has its own forcing tests. These historical idle-window tests
    # model an otherwise healthy durable presence census, not its filesystem.
    class _NoPresenceJournal:
        def unresolved_presence(self, _sandbox_id, _home):
            return []
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", _NoPresenceJournal)
    monkeypatch.setattr(
        migrate,
        "current_image",
        lambda *_: SimpleNamespace(
            tag="matrx-sandbox:bare",
            image_id="sha256:" + "b" * 64,
            version="new-version",
        ),
    )
    monkeypatch.setattr(migrate, "knob_int", AsyncMock(return_value=quiet_window))
    monkeypatch.setattr(activity.time, "monotonic", lambda: now)
    return migrate


@pytest.mark.asyncio
async def test_missing_activity_history_defers_before_docker_and_starts_observation(monkeypatch):
    """Break caught: absent process-local history was treated as idle."""
    from orchestrator import migrate

    sid = "sbx-unknown-history"
    activity._last_activity.pop(sid, None)
    activity._quiet_observation_started.pop(sid, None)
    monkeypatch.setattr(migrate, "knob_int", AsyncMock(return_value=10))
    monkeypatch.setattr(activity.time, "monotonic", lambda: 100.0)
    docker_lookup = Mock(side_effect=AssertionError("idle refusal touched Docker"))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", docker_lookup)

    result = await migrate.migrate_sandbox(sid, store=_IdleStore(), require_idle=True)

    assert result["status"] == "busy_deferred"
    assert "quiet observation began 0s ago (< 10s)" in result["reason"]
    assert activity._quiet_observation_started[sid] == 100.0
    docker_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_observed_quiet_window_reaches_hosted_engine(monkeypatch):
    """Break caught: a quiet interval observed by this process could never admit."""
    sid = "sbx-observed-quiet"
    migrate = _wire_idle_admission(monkeypatch, sid)
    activity._last_activity.pop(sid, None)
    activity._quiet_observation_started[sid] = 100.0
    engine = AsyncMock(return_value={"status": "migrated", "sandbox_id": sid})
    monkeypatch.setattr(migrate, "_migrate_hosted_ordered", engine)

    old_row = _Row(datetime.now(timezone.utc) - timedelta(days=1))
    result = await migrate.migrate_sandbox(sid, store=_IdleStore(old_row), require_idle=True)

    assert result["status"] == "migrated"
    engine.assert_awaited_once()


@pytest.mark.asyncio
async def test_completion_between_early_gate_and_fence_defers_and_releases_fence(monkeypatch):
    """Break caught: work completing in the admission race entered a quiet swap."""
    sid = "sbx-admission-completion-race"
    migrate = _wire_idle_admission(monkeypatch, sid)
    activity._last_activity.pop(sid, None)
    activity._quiet_observation_started[sid] = 100.0
    engine = AsyncMock(return_value={"status": "migrated", "sandbox_id": sid})
    monkeypatch.setattr(migrate, "_migrate_hosted_ordered", engine)
    original_mark = activity.mark_migrating

    async def mark_then_complete(sandbox_id):
        acquired = await original_mark(sandbox_id)
        if acquired:
            activity.note_activity(sandbox_id)
        return acquired

    monkeypatch.setattr(activity, "mark_migrating", mark_then_complete)

    result = await migrate.migrate_sandbox(sid, store=_IdleStore(), require_idle=True)

    assert result["status"] == "busy_deferred"
    assert "tool activity 0s ago (< 10s)" in result["reason"]
    engine.assert_not_awaited()
    assert sid not in activity._migrating


@pytest.mark.asyncio
async def test_unavailable_authoritative_heartbeat_defers_before_docker(monkeypatch):
    """Break caught: a missing heartbeat row or read failure was accepted as quiet."""
    from orchestrator import migrate

    sid = "sbx-heartbeat-unavailable"
    activity._last_activity.pop(sid, None)
    activity._quiet_observation_started[sid] = 100.0
    monkeypatch.setattr(migrate, "knob_int", AsyncMock(return_value=10))
    monkeypatch.setattr(activity.time, "monotonic", lambda: 111.0)
    docker_lookup = Mock(side_effect=AssertionError("idle refusal touched Docker"))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", docker_lookup)

    result = await migrate.migrate_sandbox(sid, store=_IdleStore(row=None), require_idle=True)

    assert result["status"] == "busy_deferred"
    assert "authoritative heartbeat is unavailable" in result["reason"]
    docker_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_failed_authoritative_heartbeat_read_defers_before_docker(monkeypatch):
    """Break caught: a heartbeat-store failure was accepted as idle."""
    from orchestrator import migrate

    sid = "sbx-heartbeat-read-failed"
    activity._last_activity.pop(sid, None)
    activity._quiet_observation_started[sid] = 100.0
    monkeypatch.setattr(migrate, "knob_int", AsyncMock(return_value=10))
    monkeypatch.setattr(activity.time, "monotonic", lambda: 111.0)
    docker_lookup = Mock(side_effect=AssertionError("idle refusal touched Docker"))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", docker_lookup)

    result = await migrate.migrate_sandbox(
        sid, store=_UnavailableIdleStore(), require_idle=True,
    )

    assert result["status"] == "busy_deferred"
    assert result["reason"] == (
        "authoritative heartbeat is unavailable; defer migration until idle is confirmed"
    )
    docker_lookup.assert_not_called()


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

    assert result["status"] == "migrated"
    assert result["sandbox_id"] == sid
    assert len(result["operation_id"]) == 32
    assert hosted.await_args.kwargs["interrupt_attached_sessions"] is True
    assert activity.is_migrating(sid) is False


@pytest.mark.asyncio
async def test_already_current_operation_is_durable_before_success(monkeypatch):
    """A lost no-op POST can still reconnect to its exact terminal receipt."""
    from orchestrator import migrate, migration_operations

    image = "sha256:" + "a" * 64
    old = SimpleNamespace(
        labels={"matrx.template": "bare"},
        attrs={
            "Image": image,
            "Config": {"Env": []},
            "HostConfig": {"Binds": ["home-volume:/home/agent:rw"]},
            "Mounts": [{
                "Type": "volume", "Name": "home-volume",
                "Destination": "/home/agent", "RW": True,
            }],
        },
    )
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda _sid: old))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: client)
    monkeypatch.setattr(migrate.settings, "host_tier", "hosted")
    monkeypatch.setattr(
        migrate, "current_image",
        lambda *_: SimpleNamespace(tag="matrx-sandbox:bare", image_id=image, version="v1"),
    )
    receipt = AsyncMock()
    monkeypatch.setattr(migration_operations, "record_terminal_operation", receipt)
    operation_id = "4" * 32

    result = await migrate.migrate_sandbox(
        "sbx-current", store=object(), operation_id=operation_id,
    )

    assert result == {
        "status": "already_current", "sandbox_id": "sbx-current",
        "version": "v1", "platform_env_changed": 0,
        "operation_id": operation_id,
    }
    receipt.assert_awaited_once_with(
        "sbx-current", operation_id, outcome="migrated", phase="already_current",
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, -1, 1.5, "1800", True])
async def test_auto_migrate_invalid_quiet_policy_fails_closed_before_docker(
    monkeypatch, value,
):
    """A malformed operator value must not even inspect Docker for drift."""
    from orchestrator import migrate, knobs

    docker_lookup = Mock(side_effect=AssertionError("invalid policy reached Docker"))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", docker_lookup)
    monkeypatch.setattr(knobs, "_raw", AsyncMock(return_value=value))

    result = await migrate.migrate_all_drifted(store=_IdleStore())

    assert result["migrated"] == []
    assert "quiet policy unavailable or invalid" in result["error"]
    docker_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_auto_migrate_captures_policy_once_and_forwards_to_each_box(monkeypatch):
    """The reaper's real batch entrypoint owns one immutable quiet interval."""
    from orchestrator import migrate, knobs

    drifted = [SimpleNamespace(sandbox_id="auto-quiet", drifted=True)]
    client = object()
    store = _IdleStore()
    migrate_one = AsyncMock(return_value={"status": "busy_deferred"})
    monkeypatch.setattr(knobs, "_raw", AsyncMock(return_value=1800))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: client)
    monkeypatch.setattr("orchestrator.versioning.compute_drift", lambda received: drifted)
    monkeypatch.setattr(migrate, "migrate_sandbox", migrate_one)

    result = await migrate.migrate_all_drifted(store=store)

    assert result == {
        "migrated": [], "deferred": ["auto-quiet"], "failed": [],
        "skipped": [], "unsupported": [],
    }
    migrate_one.assert_awaited_once_with(
        "auto-quiet", store=store,
        require_idle=True, quiet_interval=1800,
    )


@pytest.mark.asyncio
async def test_auto_quiet_interval_stays_captured_across_fenced_admission(monkeypatch):
    """A knob change after initial admission cannot shorten an automatic swap."""
    from orchestrator import migrate

    sid = "sbx-captured-auto-quiet"
    migrate = _wire_idle_admission(monkeypatch, sid, quiet_window=1)
    activity._last_activity.pop(sid, None)
    activity._quiet_observation_started[sid] = 100.0
    # Any manual-window read would prove the second fence can be shortened.
    manual_window = AsyncMock(side_effect=AssertionError("read manual window"))
    monkeypatch.setattr(migrate, "knob_int", manual_window)
    engine = AsyncMock(return_value={"status": "migrated", "sandbox_id": sid})
    monkeypatch.setattr(migrate, "_migrate_hosted_ordered", engine)
    old_row = _Row(datetime.now(timezone.utc) - timedelta(days=1))

    result = await migrate.migrate_sandbox(
        sid, store=_IdleStore(old_row), require_idle=True, quiet_interval=10,
    )

    assert result["status"] == "migrated"
    manual_window.assert_not_awaited()
    assert "quiet_interval" not in engine.await_args.kwargs


@pytest.mark.asyncio
async def test_manual_idle_migration_keeps_heartbeat_window(monkeypatch):
    """The automatic-only policy does not replace the manual 120-second gate."""
    from orchestrator import migrate

    sid = "sbx-manual-window"
    activity._last_activity.pop(sid, None)
    activity._quiet_observation_started[sid] = 100.0
    monkeypatch.setattr(activity.time, "monotonic", lambda: 111.0)
    manual_window = AsyncMock(return_value=120)
    monkeypatch.setattr(migrate, "knob_int", manual_window)
    docker_lookup = Mock(side_effect=AssertionError("manual quiet gate reached Docker"))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", docker_lookup)

    result = await migrate.migrate_sandbox(sid, store=_IdleStore(), require_idle=True)

    assert result["status"] == "busy_deferred"
    manual_window.assert_awaited_once_with("migrate_recent_heartbeat_seconds")
    docker_lookup.assert_not_called()


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
