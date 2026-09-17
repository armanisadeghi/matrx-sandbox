"""Behavioral tests for the in-memory store + warm-pool claim atomicity.

The in-memory and Postgres stores must agree on lifecycle semantics (the audit
found mark_stopped and update_heartbeat diverging). These tests pin the
in-memory behavior to the Postgres contract; the Postgres store is exercised
separately under --run-integration.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore
from tests.conftest import seed_store_sandbox_knobs

ORG_ID = "22222222-2222-4222-8222-222222222222"


def _mk(sandbox_id: str = "sbx-test00000001", user_id: str | None = None) -> SandboxResponse:
    return SandboxResponse(
        sandbox_id=sandbox_id,
        user_id=user_id or "00000000-0000-0000-0000-000000000001",
        organization_id=ORG_ID,
        status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_update_heartbeat_does_not_force_running():
    """A heartbeat stamps last_heartbeat_at but must NOT flip status to RUNNING
    (that was the in-memory-only divergence from Postgres)."""
    store = InMemorySandboxStore()
    sb = _mk()
    await store.save(sb)

    assert await store.update_heartbeat(sb.sandbox_id) is True
    got = await store.get(sb.sandbox_id)
    assert got.status == SandboxStatus.READY  # unchanged
    assert got.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_mark_stopped_records_reason_and_timestamp():
    """mark_stopped must stamp stopped_at + stop_reason, matching Postgres."""
    store = InMemorySandboxStore()
    sb = _mk()
    await store.save(sb)

    assert await store.mark_stopped(sb.sandbox_id, "expired") is True
    got = await store.get(sb.sandbox_id)
    assert got.status == SandboxStatus.STOPPED
    assert got.stop_reason == "expired"
    assert got.stopped_at is not None


@pytest.mark.asyncio
async def test_mark_stopped_unknown_returns_false():
    store = InMemorySandboxStore()
    assert await store.mark_stopped("sbx-missing00001", "admin") is False
