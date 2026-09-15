"""Durable stop/delete receipts exercise the real lease and task registry."""
from __future__ import annotations

import asyncio
import multiprocessing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore


USER = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
SID = "sbx-durable-stop"


def _hold_lifecycle_operation_lock(root: str, operation_id: str, ready, release) -> None:
    """Separate-process owner witness for orphan detection."""
    journal = HostedMigrationJournal(Path(root))
    with journal.lock("lifecycle-operation-" + operation_id):
        ready.set()
        release.wait(5)


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
    async with asyncio.timeout(2):
        while True:
            receipt = await lifecycle_operations.lifecycle_status(SID, operation, journal=journal)
            if receipt and receipt["state"] == "succeeded":
                break
            await asyncio.sleep(0.01)
    assert receipt == {"operation_id": UUID(operation).hex, "sandbox_id": SID, "row_id": str((await store.get_lifecycle(SID))["row_id"]), "kind": "stop", "state": "succeeded", "phase": "complete", "graceful": True}


@pytest.mark.asyncio
async def test_force_stop_intent_survives_durable_orphan_recovery_to_destroy(monkeypatch, tmp_path):
    """A crashed force-stop owner must not recover as a graceful teardown."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); await store.save(_row())
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    parked = asyncio.Event()

    async def abandon_owner():
        await parked.wait()

    original_start = lifecycle_operations.start_owned_operation
    task = asyncio.create_task(abandon_owner())
    async def parked_start(*_args, **_kwargs): return task
    monkeypatch.setattr(lifecycle_operations, "start_owned_operation", parked_start)
    monkeypatch.setattr(lifecycle_operations, "_runtime_is_terminal", lambda _id: _terminal_runtime())
    operation = str(uuid4())
    status, admitted = await lifecycle_operations.admit_lifecycle_operation(
        SID, operation, "stop", graceful=False, journal=journal,
    )
    assert status == 202 and admitted["graceful"] is False
    assert lifecycle_operations._read(journal, SID, operation)["graceful"] is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    orphan = await lifecycle_operations.lifecycle_status(SID, operation, journal=journal)
    assert orphan["state"] == "recovery_required" and orphan["graceful"] is False

    seen: list[bool] = []
    async def destroy(_sandbox_id, graceful, _reason, _final_status):
        seen.append(graceful)
        return await store.mark_stopped(SID, "user_requested")
    monkeypatch.setattr(lifecycle_operations, "start_owned_operation", original_start)
    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", destroy)
    status, recovery = await lifecycle_operations.admit_lifecycle_operation(
        SID, operation, "stop", recover=True, graceful=False, journal=journal,
    )
    assert status == 202 and recovery["graceful"] is False
    for _ in range(30):
        receipt = await lifecycle_operations.lifecycle_status(SID, operation, journal=journal)
        if receipt and receipt["state"] == "succeeded": break
        await asyncio.sleep(0)
    assert receipt["graceful"] is False and seen == [False]


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
        "operation_id": operation, "sandbox_id": SID, "row_id": record["row_id"], "kind": "stop",
            "state": "recovery_required", "phase": "recovery_required",
            "attention_needed": True, "reason": "operation owner disappeared; recover the same operation", "graceful": True,
    }


@pytest.mark.asyncio
async def test_real_process_owner_prevents_false_orphan_until_its_lock_releases(tmp_path):
    """Break caught: a live owner in another process became recovery-required."""
    from orchestrator import lifecycle_operations

    journal = HostedMigrationJournal(tmp_path); operation = uuid4().hex
    record = {
        "schema_version": 1, "operation_id": operation, "sandbox_id": SID,
        "row_id": str(uuid4()), "container_id": "runtime-original", "home_key": "home-durable",
        "kind": "stop", "state": "running", "phase": "stopping",
    }
    lifecycle_operations._write(journal, record)
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    process = multiprocessing.Process(
        target=_hold_lifecycle_operation_lock, args=(str(tmp_path), operation, ready, release),
    )
    process.start(); assert ready.wait(5)
    try:
        live = await lifecycle_operations.lifecycle_status(SID, operation, journal=journal)
        assert live["state"] == "running"
    finally:
        release.set(); process.join(5)
    assert process.exitcode == 0
    orphaned = await lifecycle_operations.lifecycle_status(SID, operation, journal=journal)
    assert orphaned["state"] == "recovery_required"


@pytest.mark.asyncio
async def test_admission_refuses_a_row_owned_by_another_orchestrator_tier(monkeypatch, tmp_path):
    """Break caught: hosted Docker absence terminalized an EC2 canonical row."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.config import settings

    row = _row(); row.tier = "ec2"
    store = InMemorySandboxStore(); await store.save(row)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    called = False

    def must_not_acquire(*_args):
        nonlocal called; called = True
        raise AssertionError("foreign tier must refuse before local receipt/lease admission")

    monkeypatch.setattr(lifecycle_operations, "_acquire", must_not_acquire)
    with pytest.raises(lifecycle_operations.LifecycleConflict, match="another tier"):
        await lifecycle_operations.admit_lifecycle_operation(SID, str(uuid4()), "delete", journal=HostedMigrationJournal(tmp_path))
    assert called is False


@pytest.mark.asyncio
async def test_same_operation_uuid_cannot_be_reused_for_another_sandbox(tmp_path):
    """Break caught: per-sandbox receipt paths made UUID identity non-global."""
    from orchestrator import lifecycle_operations

    journal = HostedMigrationJournal(tmp_path); operation = uuid4().hex
    lifecycle_operations._write(journal, {
        "schema_version": 1, "operation_id": operation, "sandbox_id": "sbx-first",
        "row_id": str(uuid4()), "container_id": "first-runtime", "home_key": "home-first",
        "kind": "stop", "state": "succeeded", "phase": "complete",
    })
    with pytest.raises(lifecycle_operations.LifecycleConflict, match="another lifecycle target"):
        await lifecycle_operations.admit_lifecycle_operation(SID, operation, "stop", journal=journal)


@pytest.mark.asyncio
async def test_same_operation_uuid_refuses_force_intent_mismatch(tmp_path):
    """Break caught: a retry could silently change graceful teardown intent."""
    from orchestrator import lifecycle_operations

    journal = HostedMigrationJournal(tmp_path); operation = uuid4().hex
    lifecycle_operations._write(journal, {
        "schema_version": 2, "operation_id": operation, "sandbox_id": SID,
        "row_id": str(uuid4()), "container_id": "runtime-original", "home_key": "home-durable",
        "kind": "stop", "graceful": True, "state": "succeeded", "phase": "complete",
    })
    with pytest.raises(lifecycle_operations.LifecycleConflict, match="another lifecycle intent"):
        await lifecycle_operations.admit_lifecycle_operation(SID, operation, "stop", graceful=False, journal=journal)


@pytest.mark.asyncio
async def test_cancellation_during_post_lock_identity_reread_closes_leases(monkeypatch, tmp_path):
    """Break caught: cancellation after _acquire leaked the lifecycle leases."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); await store.save(_row())
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    entered = asyncio.Event()
    calls = 0
    original_get = store.get

    async def blocked_get(sandbox_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await asyncio.Event().wait()
        return await original_get(sandbox_id)

    class Stack:
        closed = False
        def close(self): self.closed = True

    stack = Stack()
    monkeypatch.setattr(store, "get", blocked_get)
    monkeypatch.setattr(lifecycle_operations, "_acquire", lambda *_args: stack)
    task = asyncio.create_task(lifecycle_operations.admit_lifecycle_operation(SID, str(uuid4()), "stop", journal=HostedMigrationJournal(tmp_path)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stack.closed is True


@pytest.mark.asyncio
async def test_recovery_identity_conflict_fails_only_after_original_runtime_census(monkeypatch, tmp_path):
    """Break caught: recovery rebound a receipt to a replacement sandbox row."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); replacement = _row()
    replacement.row_id = uuid4(); replacement.container_id = "replacement-runtime"
    replacement.tier = "ec2"
    await store.save(replacement)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    journal = HostedMigrationJournal(tmp_path); operation = uuid4().hex
    original_row_id = str(uuid4())
    lifecycle_operations._write(journal, {
        "schema_version": 2, "operation_id": operation, "sandbox_id": SID,
        "row_id": original_row_id, "container_id": "runtime-original", "home_key": "home-durable",
        "kind": "delete", "graceful": True, "state": "recovery_required", "phase": "recovery_required",
        "reason": "operation needs recovery",
    })
    monkeypatch.setattr(lifecycle_operations, "_runtime_is_terminal", lambda _id: _terminal_runtime())

    status, receipt = await lifecycle_operations.admit_lifecycle_operation(SID, operation, "delete", recover=True, journal=journal)

    assert status == 200 and receipt["state"] == "failed" and receipt["row_id"] == original_row_id
    current = await store.get(SID)
    assert current.container_id == "replacement-runtime"
    assert (await store.get_lifecycle(SID))["deleted"] is False


@pytest.mark.asyncio
async def test_new_uuid_refuses_soft_deleted_tombstone_before_lease_admission(monkeypatch, tmp_path):
    """Break caught: a tombstone admitted a fresh destructive operation."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); await store.save(_row()); await store.soft_delete(SID)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    called = False

    def must_not_acquire(*_args):
        nonlocal called; called = True
        raise AssertionError("deleted tombstone must refuse before lifecycle lease admission")

    monkeypatch.setattr(lifecycle_operations, "_acquire", must_not_acquire)
    with pytest.raises(lifecycle_operations.LifecycleConflict, match="deleted"):
        await lifecycle_operations.admit_lifecycle_operation(SID, str(uuid4()), "delete", journal=HostedMigrationJournal(tmp_path))
    assert called is False


@pytest.mark.asyncio
async def test_home_or_tier_drift_after_handoff_never_calls_destroy(monkeypatch, tmp_path):
    """Break caught: a changed home/tier ran under the old lifecycle lease."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); row = _row(); await store.save(row)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    destroyed = []

    async def destroy(*_args):
        destroyed.append(True); return True

    async def handoff(_sandbox_id, _operation_id, factory, **_kwargs):
        current = await store.get(SID)
        current.persistence_volume = "home-replaced"
        await store.save(current)
        return asyncio.create_task(factory())

    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", destroy)
    monkeypatch.setattr(lifecycle_operations, "start_owned_operation", handoff)
    operation = str(uuid4())
    status, _ = await lifecycle_operations.admit_lifecycle_operation(SID, operation, "stop", journal=HostedMigrationJournal(tmp_path))
    assert status == 202
    for _ in range(20):
        receipt = await lifecycle_operations.lifecycle_status(SID, operation, journal=HostedMigrationJournal(tmp_path))
        if receipt and receipt["state"] == "recovery_required": break
        await asyncio.sleep(0)
    assert destroyed == [] and receipt["state"] == "recovery_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["row_id", "container_id", "persistence_volume", "tier"])
@pytest.mark.parametrize("boundary", ["pre_side_effect", "terminal_census"])
async def test_every_immutable_identity_drift_stays_attention_never_success(monkeypatch, tmp_path, field, boundary):
    """All identity witnesses fence both destroy admission and terminal proof."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); await store.save(_row())
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    destroyed = []

    async def drift():
        current = await store.get(SID)
        if field == "row_id": current.row_id = uuid4()
        elif field == "container_id": current.container_id = "replacement-runtime"
        elif field == "persistence_volume": current.persistence_volume = "home-replaced"
        else: current.tier = "ec2"
        await store.save(current)

    async def destroy(*_args):
        destroyed.append(True)
        await store.mark_stopped(SID, "user_requested")
        if boundary == "terminal_census":
            await drift()
        return True

    async def handoff(_sandbox_id, _operation_id, factory, **_kwargs):
        if boundary == "pre_side_effect":
            await drift()
        return asyncio.create_task(factory())

    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", destroy)
    monkeypatch.setattr(lifecycle_operations, "start_owned_operation", handoff)
    monkeypatch.setattr(lifecycle_operations, "_runtime_is_terminal", lambda _id: _terminal_runtime())
    operation = str(uuid4())
    status, _ = await lifecycle_operations.admit_lifecycle_operation(SID, operation, "stop", journal=HostedMigrationJournal(tmp_path))
    assert status == 202
    for _ in range(30):
        receipt = await lifecycle_operations.lifecycle_status(SID, operation, journal=HostedMigrationJournal(tmp_path))
        if receipt and receipt["state"] == "recovery_required": break
        await asyncio.sleep(0)
    assert receipt["state"] == "recovery_required"
    assert destroyed == ([] if boundary == "pre_side_effect" else [True])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stopping", "removing", "finalizing", "complete"])
async def test_each_work_phase_write_failure_persists_recovery_attention(monkeypatch, tmp_path, phase):
    """A write failure at any durable work phase never reports false success."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); await store.save(_row())
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    original_write = lifecycle_operations._write
    failed = False

    def flaky_write(journal, record):
        nonlocal failed
        if record["phase"] == phase and not failed:
            failed = True
            raise OSError("injected durable write failure")
        return original_write(journal, record)

    async def destroy(*_args): return await store.mark_stopped(SID, "user_requested")

    monkeypatch.setattr(lifecycle_operations, "_write", flaky_write)
    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", destroy)
    monkeypatch.setattr(lifecycle_operations, "_runtime_is_terminal", lambda _id: _terminal_runtime())
    operation = str(uuid4())
    status, _ = await lifecycle_operations.admit_lifecycle_operation(SID, operation, "stop", journal=HostedMigrationJournal(tmp_path))
    assert status == 202
    for _ in range(30):
        receipt = await lifecycle_operations.lifecycle_status(SID, operation, journal=HostedMigrationJournal(tmp_path))
        if receipt and receipt["state"] == "recovery_required": break
        await asyncio.sleep(0)
    assert failed is True and receipt["state"] == "recovery_required"


@pytest.mark.asyncio
async def test_terminal_store_failure_persists_recovery_attention(monkeypatch, tmp_path):
    """A terminal row-store failure is fenced rather than represented as success."""
    from orchestrator import lifecycle_operations, sandbox_manager
    from orchestrator.hosted_operation_lease import settings

    store = InMemorySandboxStore(); await store.save(_row())
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    monkeypatch.setattr(store, "soft_delete", lambda _sid: (_ for _ in ()).throw(OSError("store unavailable")))

    async def destroy(*_args): return await store.mark_stopped(SID, "user_requested")

    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", destroy)
    operation = str(uuid4())
    status, _ = await lifecycle_operations.admit_lifecycle_operation(SID, operation, "delete", journal=HostedMigrationJournal(tmp_path))
    assert status == 202
    for _ in range(30):
        receipt = await lifecycle_operations.lifecycle_status(SID, operation, journal=HostedMigrationJournal(tmp_path))
        if receipt and receipt["state"] == "recovery_required": break
        await asyncio.sleep(0)
    assert receipt["state"] == "recovery_required"


async def _terminal_runtime() -> bool:
    return True
