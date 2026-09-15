"""Durable stop/delete receipts exercise the real lease and task registry."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore


USER = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
SID = "sbx-durable-stop"


def _row() -> SandboxResponse:
    return SandboxResponse(
        row_id=uuid4(), sandbox_id=SID, user_id=USER, organization_id=ORG,
        status=SandboxStatus.RUNNING, container_id="runtime-original",
        created_at=datetime.now(timezone.utc), tier="hosted", persistence_volume="home-durable",
    )


@pytest.mark.asyncio
async def test_durable_admission_returns_before_stop_and_duplicate_joins_receipt(monkeypatch, tmp_path):
    """Break caught: response loss ran a second stop or waited for Docker."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); await store.save(_row())
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    started, release = asyncio.Event(), asyncio.Event()

    async def destroy(sandbox_id, graceful, reason, final_status):
        assert (sandbox_id, graceful, reason, final_status) == (SID, True, "user_requested", SandboxStatus.STOPPED)
        started.set(); await release.wait()
        return await store.mark_stopped(SID, "user_requested")

    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", destroy)
    monkeypatch.setattr(lifecycle_operations, "_runtime_is_terminal", lambda _id: _terminal_runtime())
    operation = str(uuid4())

    status, first = await lifecycle_operations.admit_lifecycle_operation(SID, operation, "stop", journal=journal)
    assert status == 202 and first["state"] == "accepted"
    await started.wait()
    status, duplicate = await lifecycle_operations.admit_lifecycle_operation(SID, operation, "stop", journal=journal)
    assert status == 202 and duplicate["state"] == "running"
    release.set()
    for _ in range(20):
        receipt = await lifecycle_operations.lifecycle_status(SID, operation, journal=journal)
        if receipt and receipt["state"] == "succeeded": break
        await asyncio.sleep(0)
    assert receipt == {"operation_id": UUID(operation).hex, "sandbox_id": SID, "kind": "stop", "state": "succeeded", "phase": "complete"}


@pytest.mark.asyncio
async def test_second_uuid_is_refused_while_same_home_is_durably_fenced(monkeypatch, tmp_path):
    """Break caught: an unresolved first stop let another UUID touch its home."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); await store.save(_row())
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    release = asyncio.Event()

    async def destroy(*_args):
        await release.wait(); return await store.mark_stopped(SID, "user_requested")

    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", destroy)
    monkeypatch.setattr(lifecycle_operations, "_runtime_is_terminal", lambda _id: _terminal_runtime())
    first = str(uuid4())
    await lifecycle_operations.admit_lifecycle_operation(SID, first, "stop", journal=journal)
    with pytest.raises(lifecycle_operations.LifecycleConflict):
        await lifecycle_operations.admit_lifecycle_operation(SID, str(uuid4()), "delete", journal=journal)
    release.set()


@pytest.mark.asyncio
async def test_orphaned_running_receipt_becomes_durable_recovery_attention(tmp_path):
    """Break caught: a crashed owner stayed reported as running forever."""
    from orchestrator import lifecycle_operations

    journal = HostedMigrationJournal(tmp_path)
    operation = uuid4().hex
    record = {
        "schema_version": 1, "operation_id": operation, "sandbox_id": SID,
        "row_id": str(uuid4()), "container_id": "runtime-original", "home_key": "home-durable",
        "kind": "stop", "state": "running", "phase": "stopping",
    }
    lifecycle_operations._write(journal, record)

    receipt = await lifecycle_operations.lifecycle_status(SID, operation, journal=journal)

    assert receipt == {
        "operation_id": operation, "sandbox_id": SID, "kind": "stop",
        "state": "recovery_required", "phase": "recovery_required",
        "attention_needed": True, "reason": "operation owner disappeared; recover the same operation",
    }


async def _terminal_runtime() -> bool:
    return True
