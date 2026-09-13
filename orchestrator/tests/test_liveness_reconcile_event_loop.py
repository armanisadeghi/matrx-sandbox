"""The liveness reconcile must never block the event loop that serves /health.

2026-09-13: the hosted orchestrator took the whole fleet's migration leases on
the event loop. Each lease is blocking filesystem work (four ``flock``s plus a
journal probe that ``fsync``s), so with 214 sandboxes on a host under image-build
I/O the loop stalled for 5m48s. The container healthcheck (3s budget) failed,
Traefik's Docker provider dropped the only non-healthy server, and the edge
answered "503 no available server" for ~7 minutes while the orchestrator process
was alive and answering /health in 0.4s once unblocked.

This guard fails if the fleet lease ever moves back onto the event loop.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal
from orchestrator.reconcile import reconcile_liveness

FLEET = 120
# Every lock acquisition costs this much wall time — the real condition on a
# host at 20%+ iowait, where a single fsync-ing probe took the better part of a
# second. On the event loop that is seconds of dead air; on a worker thread it
# is invisible to /health.
SLOW_LOCK_SECONDS = 0.01
# The container healthcheck allows 3s. A tenth of that is a generous ceiling for
# "the loop is still answering".
MAX_LOOP_STALL_SECONDS = 0.3


class _Store:
    def __init__(self) -> None:
        self.called: tuple | None = None

    async def list(self):
        return [
            SimpleNamespace(sandbox_id=f"sbx-{i:04d}", persistence_volume=f"home-{i:04d}",
                            tier="hosted")
            for i in range(FLEET)
        ]

    async def reconcile(self, alive_ids, *, tier, exclude_sandbox_ids=frozenset(),
                        include_sandbox_ids=None):
        self.called = (tier, exclude_sandbox_ids, include_sandbox_ids)
        return {"stopped": [], "refreshed": len(include_sandbox_ids or ())}


@pytest.mark.asyncio
async def test_liveness_reconcile_leases_the_fleet_without_stalling_health(monkeypatch, tmp_path):
    """Break caught: a fleet-sized lease sweep starving /health off the edge."""
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")
    monkeypatch.setattr("orchestrator.hosted_operation_lease.settings.host_tier", "hosted")
    monkeypatch.setattr("orchestrator.reconcile._alive_container_ids",
                        lambda *_: {f"live-{i:04d}" for i in range(FLEET)})
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: object())

    real_lock = HostedMigrationJournal.lock

    def slow_lock(self, key, *, shared=False):
        time.sleep(SLOW_LOCK_SECONDS)
        return real_lock(self, key, shared=shared)

    monkeypatch.setattr(HostedMigrationJournal, "lock", slow_lock)

    stalls: list[float] = []

    async def heartbeat() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            stalls.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    try:
        store = _Store()
        result = await reconcile_liveness(store)
    finally:
        beat.cancel()

    # The lease work really happened: every sandbox is leased and reconciled.
    assert store.called is not None, "reconcile never reached the store"
    tier, excluded, included = store.called
    assert tier == "hosted"
    assert excluded == frozenset()
    assert included is not None and len(included) == FLEET
    assert result["refreshed"] == FLEET
    # …and it cost the event loop nothing.
    assert stalls, (
        "the event loop never ticked once during the fleet lease sweep — /health "
        "would time out and Traefik would drop the only orchestrator server"
    )
    assert max(stalls) < MAX_LOOP_STALL_SECONDS, (
        f"event loop stalled {max(stalls):.2f}s during the fleet lease sweep — "
        "the container healthcheck would fail and Traefik would drop the only "
        "orchestrator server from the edge"
    )
