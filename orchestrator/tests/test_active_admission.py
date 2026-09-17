"""Forcing checks for the active-slot admission boundary.

The production implementation uses a PostgreSQL transaction/advisory lock;
these tests exercise the identical store contract with independently scheduled
callers.  The isolated-Postgres two-pool proof is intentionally selected only
when a disposable database fixture is supplied by the integration harness.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import AdmissionCapacityExceeded, InMemorySandboxStore

USER = "00000000-0000-4000-8000-000000000001"
ORG = "00000000-0000-4000-8000-000000000002"


def row(sandbox_id: str, *, user: str = USER, org: str = ORG) -> SandboxResponse:
    return SandboxResponse(
        sandbox_id=sandbox_id, user_id=user, organization_id=org,
        status=SandboxStatus.CREATING, created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_competing_admissions_keep_exact_ceiling_and_preserve_refusal() -> None:
    """Break caught: count-before-insert lets two callers exceed one slot."""
    store = InMemorySandboxStore()
    store.seed_feature_knobs("infrastructure.sandbox", {"active_sandbox_capacity": 1})

    async def reserve(sid: str):
        try:
            await store.reserve_active(row(sid))
            return "admitted"
        except AdmissionCapacityExceeded as exc:
            return exc

    first, second = await asyncio.gather(reserve("sbx-hosted"), reserve("sbx-ec2"))
    assert sorted(type(item).__name__ if isinstance(item, Exception) else item for item in (first, second)) == [
        "AdmissionCapacityExceeded", "admitted"
    ]
    active = await store.list(user_id=USER)
    assert [item.sandbox_id for item in active] in (["sbx-hosted"], ["sbx-ec2"])


@pytest.mark.asyncio
async def test_replacement_excludes_only_callers_exact_active_predecessor() -> None:
    """Break caught: a forged predecessor can borrow another user's slot."""
    store = InMemorySandboxStore()
    store.seed_feature_knobs("infrastructure.sandbox", {"active_sandbox_capacity": 1})
    predecessor = row("sbx-old")
    await store.reserve_active(predecessor)

    await store.reserve_active(row("sbx-next"), replacement_for="sbx-old")
    assert {item.sandbox_id for item in await store.list(user_id=USER)} == {"sbx-old", "sbx-next"}

    with pytest.raises(RuntimeError, match="replacement predecessor"):
        await store.reserve_active(row("sbx-forged"), replacement_for="sbx-other")


@pytest.mark.asyncio
async def test_legacy_resume_reserves_capacity_before_starting_runtime() -> None:
    """Break caught: retained-layer resume bypasses the active-slot ceiling."""
    store = InMemorySandboxStore()
    store.seed_feature_knobs("infrastructure.sandbox", {"active_sandbox_capacity": 1})
    await store.reserve_active(row("sbx-occupied"))
    legacy = row("sbx-legacy")
    legacy.status = SandboxStatus.STOPPED
    legacy.container_id = "container-legacy"
    await store.save(legacy)

    with pytest.raises(AdmissionCapacityExceeded) as refused:
        await store.resume_active(legacy)

    assert (refused.value.ceiling, refused.value.occupied) == (1, 1)
    assert (await store.get("sbx-legacy")).status == SandboxStatus.STOPPED


@pytest.mark.asyncio
async def test_reset_successor_lease_spans_reservation_until_runtime_creation(
    monkeypatch, tmp_path,
) -> None:
    """Break caught: reconcile terminalizes reset successor in destroy/wipe gap."""
    from orchestrator import hosted_operation_lease as leases, sandbox_manager
    from orchestrator.config import settings
    from orchestrator.hosted_migration import HostedMigrationJournal
    from orchestrator.storage_layout import user_volume_name

    store = InMemorySandboxStore()
    store.seed_feature_knobs("infrastructure.sandbox", {"active_sandbox_capacity": 1})
    predecessor = row("sbx-old")
    predecessor.status = SandboxStatus.READY
    predecessor.tier = "hosted"
    predecessor.persistence_volume = user_volume_name(USER, ORG)
    await store.save(predecessor)
    monkeypatch.setattr(sandbox_manager, "_store", store)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr(leases, "HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)

    async with sandbox_manager.reset_successor_admission(
        predecessor, name=None, config={}, template="slim",
        template_version=None, tier="hosted", labels=None, ttl_seconds=7200,
    ) as successor:
        assert (await store.get(successor.sandbox_id)).status == SandboxStatus.CREATING
        with pytest.raises(leases.HostedOperationDenied):
            with leases.hosted_operation_lease_sync(
                successor.sandbox_id, predecessor.persistence_volume,
                journal=journal, deployment=True,
            ):
                pass

    with leases.hosted_operation_lease_sync(
        successor.sandbox_id, predecessor.persistence_volume,
        journal=journal, deployment=True,
    ):
        pass


@pytest.mark.asyncio
async def test_capacity_refusal_precedes_every_runtime_mutation(monkeypatch, tmp_path) -> None:
    """Break caught: refused cold create still touches Docker or storage."""
    from orchestrator import hosted_operation_lease as leases, sandbox_manager
    from orchestrator.config import settings
    from orchestrator.hosted_migration import HostedMigrationJournal

    store = InMemorySandboxStore()
    store.seed_feature_knobs("infrastructure.sandbox", {"active_sandbox_capacity": 1})
    occupied = row("sbx-occupied")
    occupied.status = SandboxStatus.READY
    occupied.tier = "hosted"
    await store.save(occupied)
    monkeypatch.setattr(sandbox_manager, "_store", store)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr(leases, "HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr(
        sandbox_manager, "_get_docker_client",
        lambda: (_ for _ in ()).throw(AssertionError("Docker mutated before admission")),
    )
    monkeypatch.setattr(
        sandbox_manager, "ensure_user_volume",
        lambda *_args: (_ for _ in ()).throw(AssertionError("storage mutated before admission")),
    )

    with pytest.raises(AdmissionCapacityExceeded):
        await sandbox_manager.create_sandbox(
            user_id=USER, organization_id=ORG, tier="hosted", template="slim",
        )

    assert {item.sandbox_id for item in await store.list(user_id=USER)} == {"sbx-occupied"}
