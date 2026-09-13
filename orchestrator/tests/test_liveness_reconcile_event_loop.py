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
from orchestrator.hosted_runtime import recover_hosted_migrations
from orchestrator.reconcile import reconcile_from_docker, reconcile_liveness
from orchestrator.store import InMemorySandboxStore

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
async def test_boot_recovery_reads_large_journals_without_stalling_health(monkeypatch, tmp_path):
    """A large durable migration receipt must be parsed off the event loop."""
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr("orchestrator.hosted_runtime.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "hosted")
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: object())

    def slow_records():
        time.sleep(0.5)
        return []

    monkeypatch.setattr(journal, "recovery_records", slow_records)
    result, stalls = await _max_stall_while(recover_hosted_migrations(store=_Store()))

    assert result == {"recovered": [], "failed": []}
    assert stalls
    assert max(stalls) < MAX_LOOP_STALL_SECONDS, (
        f"event loop stalled {max(stalls):.2f}s while reading migration journals"
    )


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


USER = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"


# The discovery sweep already yields between containers, so its exposure is one
# container's worth of blocking work at a time — smaller fleet, slower locks.
DISCOVERY_FLEET = 8
DISCOVERY_LOCK_SECONDS = 0.1


def _container(index: int):
    return SimpleNamespace(
        id=f"runtime-{index:04d}",
        attrs={
            "Config": {"Labels": {
                "matrx.sandbox_id": f"sbx-{index:04d}", "matrx.user_id": USER,
                "matrx.organization_id": ORG, "matrx.tier": "hosted",
            }},
            "State": {"Running": True, "Status": "running"},
            "Mounts": [{"Type": "volume", "Name": f"home-{index:04d}",
                        "Destination": "/home/agent"}],
            "NetworkSettings": {"Ports": {}},
        },
        reload=lambda: None,
    )


async def _max_stall_while(coro):
    """Run ``coro`` and return the longest gap between event-loop ticks."""
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
        result = await coro
    finally:
        beat.cancel()
    return result, stalls


@pytest.mark.asyncio
async def test_discovery_reconcile_leases_each_container_without_stalling_health(monkeypatch, tmp_path):
    """Break caught: the discovery sweep's per-container lease back on the loop.

    Same class as the liveness sweep: discovery takes a home lease and reads the
    migration fence for every container it finds. Both are blocking journal I/O.
    """
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.reconcile.settings.host_tier", "hosted")
    monkeypatch.setattr("orchestrator.hosted_operation_lease.settings.host_tier", "hosted")

    containers = [_container(i) for i in range(DISCOVERY_FLEET)]
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client",
                        lambda: SimpleNamespace(
                            containers=SimpleNamespace(list=lambda **_: containers)))

    real_lock = HostedMigrationJournal.lock

    def slow_lock(self, key, *, shared=False):
        time.sleep(DISCOVERY_LOCK_SECONDS)
        return real_lock(self, key, shared=shared)

    monkeypatch.setattr(HostedMigrationJournal, "lock", slow_lock)

    store = InMemorySandboxStore()
    summary, stalls = await _max_stall_while(reconcile_from_docker(store))

    assert summary["reconciled"] == DISCOVERY_FLEET, summary
    assert stalls, (
        "the event loop never ticked once during the discovery sweep — /health "
        "would time out and Traefik would drop the only orchestrator server"
    )
    assert max(stalls) < MAX_LOOP_STALL_SECONDS, (
        f"event loop stalled {max(stalls):.2f}s during the discovery sweep"
    )
