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


@pytest.mark.asyncio
async def test_concurrent_claim_does_not_double_assign_warm_box(monkeypatch):
    """Two simultaneous claim_warm calls must never adopt the same container."""
    from orchestrator import pool

    store = InMemorySandboxStore()
    monkeypatch.setattr(pool, "_pool_enabled", lambda: True)
    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: store)
    monkeypatch.setattr("orchestrator.sandbox_manager._proxy_url_for", lambda sid: None)
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: object())

    # Two distinct fake warm containers; only ONE should ever be claimed by each
    # user, and never the same one twice.
    class _FakeContainer:
        def __init__(self, sid):
            self.id = f"cid-{sid}"
            self.attrs = {
                "Config": {"Labels": {"matrx.sandbox_id": sid, "matrx.template": "slim"}},
                "NetworkSettings": {"Ports": {"22/tcp": [{"HostPort": "30001"}]}},
            }
        def reload(self):
            pass

    warm = [_FakeContainer("sbx-warm00000001"), _FakeContainer("sbx-warm00000002")]

    async def fake_unclaimed(template):
        # Mirror the real filter: exclude boxes that already have a row.
        out = []
        for c in warm:
            sid = c.attrs["Config"]["Labels"]["matrx.sandbox_id"]
            if await store.get(sid) is None:
                out.append(c)
        return out

    monkeypatch.setattr(pool, "_unclaimed_warm", fake_unclaimed)
    # Memory hydrate + replenish are best-effort; stub them out.
    monkeypatch.setattr("orchestrator.memory_sync.hydrate_memory_into_container",
                        lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(pool, "_replenish_async", lambda: asyncio.sleep(0))

    results = await asyncio.gather(
        pool.claim_warm(
            "00000000-0000-0000-0000-0000000000aa", ORG_ID, template="slim"
        ),
        pool.claim_warm(
            "00000000-0000-0000-0000-0000000000bb", ORG_ID, template="slim"
        ),
    )

    claimed_ids = sorted(r.sandbox_id for r in results if r is not None)
    # Both claims succeeded against DIFFERENT boxes (no double-assignment).
    assert claimed_ids == ["sbx-warm00000001", "sbx-warm00000002"]
    # And each user owns exactly one distinct box.
    owners = {r.sandbox_id: r.user_id for r in results if r}
    assert len(set(owners.values())) == 2
