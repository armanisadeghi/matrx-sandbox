"""EC2 durable-home lifecycle regression tests."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.hosted_migration import HostedMigrationJournal
from orchestrator.storage_layout import ec2_home_volume_name, validate_ec2_home_volume
from orchestrator.store import InMemorySandboxStore


USER = "00000000-0000-4000-8000-000000000001"
ORG = "22222222-2222-4222-8222-222222222222"
SID = "sbx-0123456789ab"


def _row(*, persistence_volume: str | None = None) -> SandboxResponse:
    return SandboxResponse(
        sandbox_id=SID, user_id=USER, organization_id=ORG, tier="ec2",
        status=SandboxStatus.READY, container_id="container-exact",
        created_at=datetime.now(timezone.utc), persistence_volume=persistence_volume,
    )


def _volume(reference: str, *, user: str = USER):
    return SimpleNamespace(attrs={
        "Name": reference, "Driver": "local", "Labels": {
            "matrx.owner": "orchestrator", "matrx.sandbox_id": SID,
            "matrx.user_id": user, "matrx.organization_id": ORG,
            "matrx.kind": "ec2-home", "matrx.tier": "ec2",
        },
    }, reload=lambda: None)


def test_ec2_reused_home_refuses_missing_and_foreign_volume():
    reference = ec2_home_volume_name(SID)
    row = _row(persistence_volume=reference)

    class Missing:
        class volumes:
            @staticmethod
            def get(_): raise RuntimeError("not found")

    with pytest.raises(RuntimeError, match="missing"):
        validate_ec2_home_volume(Missing(), reference, row)

    class Foreign:
        class volumes:
            @staticmethod
            def get(_): return _volume(reference, user="different-user")

    with pytest.raises(RuntimeError, match="labels"):
        validate_ec2_home_volume(Foreign(), reference, row)


@pytest.mark.asyncio
async def test_legacy_ec2_destroy_retains_exact_layer_and_resume_restarts_it(monkeypatch):
    from orchestrator import sandbox_manager

    @asynccontextmanager
    async def lease(*_args, **_kwargs):
        yield

    monkeypatch.setattr("orchestrator.hosted_operation_lease.hosted_operation_lease", lease)
    store = InMemorySandboxStore()
    row = _row()
    await store.save(row)
    monkeypatch.setattr(sandbox_manager, "_store", store)

    class Container:
        id = "container-exact"
        status = "running"
        def stop(self, **_): self.status = "exited"
        def remove(self, **_): raise AssertionError("legacy EC2 layer must not be removed")
        def reload(self): pass
        def start(self): self.status = "running"
        def exec_run(self, _): return (0, b"")

    container = Container()
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda identity: container if identity in {SID, "container-exact"} else None))
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)
    monkeypatch.setattr(sandbox_manager, "knob_int", lambda _name: __import__("asyncio").sleep(0, result=1))
    monkeypatch.setattr("orchestrator.memory_sync.capture_memory_from_container", lambda *_: __import__("asyncio").sleep(0))

    assert await sandbox_manager._destroy_sandbox_unleased(SID) is True
    assert container.status == "exited"
    resumed = await sandbox_manager.resume_retained_ec2_layer(await store.get(SID))
    assert resumed.sandbox_id == SID
    assert resumed.container_id == "container-exact"
    assert resumed.status == SandboxStatus.READY


@pytest.mark.asyncio
async def test_ec2_named_reset_passes_prior_row_to_keep_exact_home(monkeypatch):
    from orchestrator.routes import sandboxes

    reference = ec2_home_volume_name(SID)
    old = _row(persistence_volume=reference)
    captured = {}

    async def get(_): return old
    async def destroy(*_args, **kwargs):
        captured["stop_reason"] = kwargs["reason"]
        return True
    async def create(**kwargs):
        captured.update(kwargs)
        return old

    monkeypatch.setattr(sandboxes, "_migration_fenced", lambda _: __import__("asyncio").sleep(0, result=False))
    monkeypatch.setattr(sandboxes.sandbox_manager, "get_sandbox", get)
    monkeypatch.setattr(sandboxes.sandbox_manager, "destroy_sandbox", destroy)
    monkeypatch.setattr(sandboxes.sandbox_manager, "create_sandbox", create)

    result = await sandboxes.reset_sandbox(SID)
    assert result is old
    assert captured["persistence_from"] == SID
    assert captured["stop_reason"] == "user_requested"


@pytest.mark.asyncio
async def test_expiry_that_loses_to_real_legacy_resume_never_stops_the_resumed_container(monkeypatch, tmp_path):
    """Break caught: a stale expiry decision tears down the exact layer after resume made it ready."""
    from orchestrator import reaper, sandbox_manager

    store = InMemorySandboxStore()
    row = _row()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await store.save(row)
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr(sandbox_manager.settings, "host_tier", "ec2")
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr(sandbox_manager, "_store", store)

    class Container:
        id = "container-exact"
        status = "exited"
        def reload(self): pass
        def start(self): self.status = "running"

    container = Container()
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda identity: container if identity == container.id else None))
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)

    async def ready(sandbox):
        sandbox.status = SandboxStatus.READY
        return sandbox

    monkeypatch.setattr(sandbox_manager, "_wait_for_ready", ready)
    original_expire = store.expire_stale
    original_get = store.get
    reaper_read_started = __import__("asyncio").Event()
    resumed_ready = __import__("asyncio").Event()

    async def resume_after_expiry():
        await reaper_read_started.wait()
        from orchestrator.hosted_operation_lease import HostedOperationDenied
        while True:
            try:
                resumed = await sandbox_manager.resume_retained_ec2_layer(await original_get(SID))
                resumed_ready.set()
                return resumed
            except HostedOperationDenied:
                # The expiry decision holds a shared admission lease. Model a
                # retrying resume request that wins immediately after release.
                await __import__("asyncio").sleep(0)

    resume_task = __import__("asyncio").create_task(resume_after_expiry())

    async def yielding_get(sandbox_id):
        # Let the scheduled resume acquire the newly released exclusive lease
        # before the reaper attempts its stale exclusive teardown admission.
        if __import__("asyncio").current_task() is resume_task:
            return await original_get(sandbox_id)
        reaper_read_started.set()
        await resumed_ready.wait()
        return await original_get(sandbox_id)

    async def expire(**kwargs):
        expired = await original_expire(**kwargs)
        reaper_read_started.set()
        return expired

    store.get = yielding_get
    store.expire_stale = expire
    stopped = []

    async def stale_teardown(*_args, **_kwargs):
        stopped.append(True)
        return True

    monkeypatch.setattr(sandbox_manager, "_destroy_sandbox_unleased", stale_teardown)

    summary = await reaper._reap_once()
    resumed = await resume_task
    assert stopped == []
    assert resumed.status == SandboxStatus.READY
    assert (await store.get(SID)).status == SandboxStatus.READY
    assert summary["torn_down"] == 0


@pytest.mark.asyncio
async def test_purge_refuses_legacy_row_when_resume_wins_after_destroy(monkeypatch, tmp_path):
    """Break caught: DELETE purge soft-deletes the same legacy id after it restarted."""
    from orchestrator import sandbox_manager
    from orchestrator.routes import sandboxes

    store = InMemorySandboxStore()
    row = _row()
    row.status = SandboxStatus.STOPPED
    await store.save(row)
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr(sandbox_manager.settings, "host_tier", "ec2")
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr(sandbox_manager, "_store", store)

    async def not_migrating(_): return False
    async def get(_): return await store.get(SID)
    async def destroy(*_args, **_kwargs):
        row.status = SandboxStatus.READY
        await store.save(row)
        return True

    monkeypatch.setattr(sandboxes, "_migration_fenced", not_migrating)
    monkeypatch.setattr(sandboxes.sandbox_manager, "get_sandbox", get)
    monkeypatch.setattr(sandboxes.sandbox_manager, "destroy_sandbox", destroy)

    with pytest.raises(HTTPException, match="resumed or changed") as refusal:
        await sandboxes.destroy_sandbox(SID, purge=True)
    assert refusal.value.status_code == 409
    assert (await store.get_lifecycle(SID))["deleted"] is False
