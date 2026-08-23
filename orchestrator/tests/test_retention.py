"""Retention model: soft-deleted rows vanish from default lists; finished
rows age out after terminal_retention_days. Pins the in-memory store to the
Postgres contract (Postgres exercised under --run-integration)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore


def _mk(sandbox_id: str, status=SandboxStatus.READY, stopped_days_ago: int | None = None):
    sb = SandboxResponse(
        sandbox_id=sandbox_id,
        user_id="00000000-0000-0000-0000-000000000001",
        organization_id="22222222-2222-4222-8222-222222222222",
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    if stopped_days_ago is not None:
        sb.stopped_at = datetime.now(timezone.utc) - timedelta(days=stopped_days_ago)
    return sb


@pytest.mark.asyncio
async def test_soft_delete_hides_from_default_list_but_keeps_row():
    store = InMemorySandboxStore()
    await store.save(_mk("sbx-a"))
    await store.save(_mk("sbx-b"))

    assert await store.soft_delete("sbx-a") is True
    assert await store.soft_delete("sbx-a") is False  # idempotent

    default = {s.sandbox_id for s in await store.list()}
    assert default == {"sbx-b"}
    everything = {s.sandbox_id for s in await store.list(include_deleted=True)}
    assert everything == {"sbx-a", "sbx-b"}
    # Row survives for audit; lifecycle reports deleted (zombie reap keys off this).
    assert await store.get("sbx-a") is not None
    assert (await store.get_lifecycle("sbx-a"))["deleted"] is True


@pytest.mark.asyncio
async def test_purge_terminal_ages_out_only_old_finished_rows():
    store = InMemorySandboxStore()
    await store.save(_mk("sbx-live", SandboxStatus.RUNNING))
    await store.save(_mk("sbx-fresh-stop", SandboxStatus.STOPPED, stopped_days_ago=1))
    await store.save(_mk("sbx-old-stop", SandboxStatus.STOPPED, stopped_days_ago=10))
    await store.save(_mk("sbx-old-expired", SandboxStatus.EXPIRED, stopped_days_ago=10))

    purged = await store.purge_terminal_older_than(7)
    assert set(purged) == {"sbx-old-stop", "sbx-old-expired"}
    # Second sweep is a no-op — already soft-deleted.
    assert await store.purge_terminal_older_than(7) == []

    remaining = {s.sandbox_id for s in await store.list()}
    assert remaining == {"sbx-live", "sbx-fresh-stop"}
