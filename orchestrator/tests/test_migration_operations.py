import asyncio
from pathlib import Path

import pytest

from orchestrator.migration_operations import (
    active_operation,
    migration_status,
    record_terminal_operation,
    reset_operation_registry_for_tests,
    run_owned_operation,
)
from orchestrator.hosted_migration import HostedMigrationJournal


def record(operation_id="a" * 32, phase="target_start_intent", cleanup_complete=False):
    value = {
        "schema_version": 1, "sandbox_id": "sbx", "old_id": "old", "old_name": "sbx",
        "old_image": "sha256:" + "a" * 64, "source_volume": "home",
        "source_identity": {"name": "home"}, "row_identity": {"sandbox_id": "sbx"},
        "target_name": "sbx-mig-op", "target_image": "sha256:" + "b" * 64,
        "target_id": "new", "operation_label": operation_id, "backup_name": "backup",
        "backup_receipt": {"verified": True}, "helper_image": "sha256:" + "c" * 64,
        "pre_cas_home_receipt": {"verified": True},
        "rollback_name": "sbx-old-op", "verify_timeout": 1, "stop_timeout": 1,
        "source_endpoint": {"network": "bridge", "network_id": "network-id"},
        "network_disconnect_receipt": {"old_id": "old", "network_id": "network-id"},
        "phase": phase,
    }
    if cleanup_complete:
        value["cleanup_complete"] = True
    return value


@pytest.fixture(autouse=True)
def clean_registry():
    reset_operation_registry_for_tests()
    yield
    reset_operation_registry_for_tests()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_owned_migration():
    """Break caught: HTTP disconnect canceled the durable operation it was awaiting."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()
        return {"status": "migrated", "sandbox_id": "sbx"}

    waiter = asyncio.create_task(run_owned_operation("sbx", "a" * 32, work))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert active_operation("sbx") is not None
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active_operation("sbx") is None


@pytest.mark.asyncio
async def test_same_operation_reconnects_to_one_task_and_echoes_identity():
    calls = 0
    release = asyncio.Event()

    async def work():
        nonlocal calls
        calls += 1
        await release.wait()
        return {"status": "migrated", "sandbox_id": "sbx"}

    first = asyncio.create_task(run_owned_operation("sbx", "b" * 32, work))
    await asyncio.sleep(0)
    second = asyncio.create_task(run_owned_operation("sbx", "b" * 32, work))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    assert await first == await second == {
        "status": "migrated", "sandbox_id": "sbx", "operation_id": "b" * 32,
    }


@pytest.mark.asyncio
async def test_migrate_entrypoint_joins_same_id_before_runtime_prechecks(monkeypatch):
    """Reconnect after rename cannot fall into a second Docker lookup/not-found path."""
    from orchestrator import migrate

    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"status": "migrated", "sandbox_id": "sbx"}

    monkeypatch.setattr(migrate, "_migrate_sandbox_once", once)
    first = asyncio.create_task(migrate.migrate_sandbox(
        "sbx", store=object(), operation_id="5" * 32,
    ))
    await started.wait()
    second = asyncio.create_task(migrate.migrate_sandbox(
        "sbx", store=object(), operation_id="5" * 32,
    ))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    assert await first == await second


@pytest.mark.asyncio
async def test_different_operation_is_refused_without_starting_second_task():
    calls = []
    release = asyncio.Event()
    started = asyncio.Event()

    async def first_work():
        calls.append("first")
        started.set()
        await release.wait()
        return {"status": "migrated", "sandbox_id": "sbx"}

    async def forbidden_work():
        calls.append("second")
        return {"status": "migrated", "sandbox_id": "sbx"}

    first = asyncio.create_task(run_owned_operation("sbx", "c" * 32, first_work))
    await started.wait()
    refused = await run_owned_operation("sbx", "d" * 32, forbidden_work)
    assert refused["status"] == "busy_deferred"
    assert refused["operation_id"] == "d" * 32
    assert calls == ["first"]
    release.set()
    await first


@pytest.mark.asyncio
async def test_registry_reports_recovery_liveness_distinctly():
    release = asyncio.Event()

    async def work():
        await release.wait()
        return {"status": "recovered", "sandbox_id": "sbx"}

    waiter = asyncio.create_task(
        run_owned_operation("sbx", "e" * 32, work, kind="recovering")
    )
    await asyncio.sleep(0)
    entry = active_operation("sbx")
    assert entry is not None
    assert entry.operation_id == "e" * 32 and entry.kind == "recovering"
    release.set()
    await waiter


@pytest.mark.asyncio
async def test_startup_recovery_is_registered_as_recovering(monkeypatch):
    """A reconnect sees boot recovery as live work, not an orphaned failure."""
    from orchestrator import hosted_runtime
    from orchestrator.config import settings

    operation_id = "f" * 32
    value = record(operation_id=operation_id)

    class Journal:
        def ensure_ready(self):
            return None

        def records(self):
            return [value]

    observed = []

    async def recover(received, **_kwargs):
        entry = active_operation("sbx")
        observed.append((received["operation_label"], entry.kind if entry else None))
        return {"status": "recovered", "sandbox_id": "sbx"}

    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(hosted_runtime, "HostedMigrationJournal", Journal)
    monkeypatch.setattr(
        "orchestrator.sandbox_manager._get_docker_client", lambda: object()
    )
    monkeypatch.setattr(hosted_runtime, "recover_hosted_migration", recover)

    result = await hosted_runtime.recover_hosted_migrations(store=object())

    assert result == {"recovered": ["sbx"], "failed": []}
    assert observed == [(operation_id, "recovering")]
    assert active_operation("sbx") is None


@pytest.mark.asyncio
async def test_admitted_interruption_finishes_recovery_before_propagating_cancellation(
    monkeypatch,
):
    """A second cancellation cannot release the host lock ahead of recovery."""
    from orchestrator import hosted_runtime

    started = asyncio.Event()
    release = asyncio.Event()
    recovered = asyncio.Event()
    recorded = []

    def record_error(received, _journal, interruption):
        recorded.append((received["operation_label"], str(interruption)))

    async def recover(received, **_kwargs):
        started.set()
        await release.wait()
        recovered.set()
        return {"status": "recovered", "sandbox_id": received["sandbox_id"]}

    monkeypatch.setattr(hosted_runtime, "_record_error", record_error)
    monkeypatch.setattr(hosted_runtime, "recover_hosted_migration", recover)
    value = record(operation_id="9" * 32)
    task = asyncio.create_task(hosted_runtime._recover_admitted_interruption(
        value, store=object(), client=object(), journal=object(),
        interruption=hosted_runtime.HostedMigrationStateError("cancelled"),
    ))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not recovered.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recovered.is_set()
    assert recorded == [("9" * 32, "cancelled")]


@pytest.mark.asyncio
async def test_exact_terminal_status_never_becomes_unrelated_current_success(tmp_path):
    journal = HostedMigrationJournal(tmp_path)
    journal.write(record(phase="committed", cleanup_complete=True))
    assert (await migration_status("sbx", "a" * 32, journal=journal))["outcome"] == "migrated"
    assert (await migration_status("sbx", "b" * 32, journal=journal))["outcome"] == "idle"
    assert (await migration_status("sbx", journal=journal))["outcome"] == "idle"


@pytest.mark.asyncio
async def test_noop_terminal_receipt_survives_registry_completion(tmp_path):
    journal = HostedMigrationJournal(tmp_path)
    operation_id = "7" * 32
    await record_terminal_operation(
        "sbx", operation_id, outcome="migrated", phase="already_current",
        journal=journal,
    )
    assert await migration_status("sbx", operation_id, journal=journal) == {
        "sandbox_id": "sbx", "operation_id": operation_id,
        "outcome": "migrated", "execution_state": "complete",
        "phase": "already_current",
    }
    assert (await migration_status("sbx", "6" * 32, journal=journal))["outcome"] == "idle"


@pytest.mark.asyncio
async def test_no_id_status_never_combines_active_operation_with_old_journal(tmp_path):
    journal = HostedMigrationJournal(tmp_path)
    journal.write(record(operation_id="1" * 32, phase="committed", cleanup_complete=True))
    release = asyncio.Event()

    async def work():
        await release.wait()
        return {"status": "migrated", "sandbox_id": "sbx"}

    waiter = asyncio.create_task(run_owned_operation("sbx", "2" * 32, work))
    await asyncio.sleep(0)
    status = await migration_status("sbx", journal=journal)
    assert status == {
        "sandbox_id": "sbx", "operation_id": "2" * 32,
        "outcome": "in_progress", "execution_state": "running",
        "phase": "admitting",
    }
    release.set()
    await waiter


@pytest.mark.asyncio
async def test_unowned_nonterminal_journal_requires_recovery_without_raw_error(tmp_path):
    journal = HostedMigrationJournal(tmp_path)
    value = record()
    value["last_error"] = "OSError: secret path /srv/private/file"
    journal.write(value)
    status = await migration_status("sbx", "a" * 32, journal=journal)
    assert status == {
        "sandbox_id": "sbx", "operation_id": "a" * 32,
        "outcome": "recovery_required", "execution_state": "unowned",
        "phase": "target_start_intent",
        "reason": "The update outcome requires orchestrator recovery. Do not start another update.",
    }
    assert "/srv/private" not in str(status)


@pytest.mark.asyncio
async def test_cleanup_incomplete_terminal_phase_is_not_hidden_as_idle(tmp_path):
    journal = HostedMigrationJournal(tmp_path)
    value = record(phase="recovered", cleanup_complete=False)
    journal.write(value)
    status = await migration_status("sbx", journal=journal)
    assert status["outcome"] == "recovery_required"
    assert status["phase"] == "recovered"


@pytest.mark.asyncio
async def test_status_reads_atomic_journal_without_acquiring_mutation_lock(tmp_path):
    journal = HostedMigrationJournal(tmp_path)
    journal.write(record())
    with journal.lock("sbx"):
        status = await asyncio.wait_for(
            migration_status("sbx", "a" * 32, journal=journal), timeout=1,
        )
    assert status["outcome"] == "recovery_required"


@pytest.mark.asyncio
async def test_large_manifest_status_is_projected_off_event_loop(tmp_path, monkeypatch):
    journal = HostedMigrationJournal(tmp_path)
    value = record()
    value["backup_receipt"]["manifest"] = "x" * 2_000_000
    journal.write(value)
    ticks = []
    original = journal.read

    def slow_read(sandbox_id):
        import time
        time.sleep(0.05)
        return original(sandbox_id)

    monkeypatch.setattr(journal, "read", slow_read)
    task = asyncio.create_task(migration_status("sbx", "a" * 32, journal=journal))
    await asyncio.sleep(0.01)
    ticks.append("event-loop-ran")
    status = await task
    assert ticks == ["event-loop-ran"] and status["phase"] == "target_start_intent"
    assert "manifest" not in status and len(str(status)) < 600


@pytest.mark.asyncio
async def test_corrupt_journal_is_not_reported_idle_or_leaked(tmp_path):
    path = Path(tmp_path) / "sbx.json"
    path.write_text('{"secret":"/srv/private"')
    status = await migration_status("sbx", "a" * 32, journal=HostedMigrationJournal(tmp_path))
    assert status["outcome"] == "recovery_required"
    assert status["phase"] == "unreadable" and "/srv/private" not in str(status)
