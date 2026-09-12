"""Real-flock guards for hosted_operation_lease."""
from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError
from orchestrator.hosted_operation_lease import HostedOperationDenied, hosted_operation_lease


def _record(sandbox_id: str, volume: str) -> dict:
    return {
        "schema_version": 1, "sandbox_id": sandbox_id, "phase": "admitted",
        "old_id": "old", "old_name": sandbox_id, "old_image": "sha256:" + "a" * 64,
        "source_volume": volume, "source_identity": {"name": volume},
        "row_identity": {"sandbox_id": sandbox_id}, "target_name": f"{sandbox_id}-mig-op",
        "target_image": "sha256:" + "b" * 64, "operation_label": "op",
        "backup_name": "backup", "helper_image": "sha256:" + "c" * 64,
        "rollback_name": f"{sandbox_id}-old-op", "verify_timeout": 1, "stop_timeout": 1,
    }


def _exclusive_try(root: str, key: str, queue) -> None:
    journal = HostedMigrationJournal(Path(root))
    try:
        with journal.lock(key):
            queue.put("acquired")
    except HostedMigrationStateError:
        queue.put("blocked")


def _exclusive_hold(root: str, key: str, ready, release) -> None:
    journal = HostedMigrationJournal(Path(root))
    with journal.lock(key):
        ready.set(); release.wait(5)


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    from orchestrator.hosted_operation_lease import settings
    monkeypatch.setattr(settings, "host_tier", "hosted")
    return HostedMigrationJournal(tmp_path)


@pytest.mark.asyncio
async def test_shared_operation_blocks_an_exclusive_migration_lock(hosted):
    """Break caught: operation only checks a lock then releases before its work."""
    queue = multiprocessing.Queue()
    async with hosted_operation_lease("box-a", "home-a", journal=hosted):
        process = multiprocessing.Process(target=_exclusive_try, args=(str(hosted.root), "volume-home-a", queue))
        process.start(); process.join(5)
        assert queue.get(timeout=1) == "blocked"


@pytest.mark.asyncio
async def test_exclusive_migration_lock_denies_new_operation(hosted):
    """Break caught: new work enters after migration has already fenced its home."""
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    process = multiprocessing.Process(target=_exclusive_hold, args=(str(hosted.root), "volume-home-a", ready, release))
    process.start(); assert ready.wait(3)
    try:
        with pytest.raises(HostedOperationDenied):
            async with hosted_operation_lease("box-a", "home-a", journal=hosted):
                pass
    finally:
        release.set(); process.join(5)


@pytest.mark.asyncio
async def test_unrelated_home_can_operate_while_other_home_is_exclusive(hosted):
    """Break caught: one migration globally stops the hosted fleet."""
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    process = multiprocessing.Process(target=_exclusive_hold, args=(str(hosted.root), "volume-home-a", ready, release))
    process.start(); assert ready.wait(3)
    try:
        async with hosted_operation_lease("box-b", "home-b", journal=hosted):
            assert True
    finally:
        release.set(); process.join(5)


@pytest.mark.asyncio
async def test_pending_sibling_home_record_denies_even_when_sandbox_ids_differ(hosted):
    """Break caught: only migrated ID is fenced, allowing a sibling to write its home."""
    hosted.write(_record("box-old", "home-a"))
    with pytest.raises(HostedOperationDenied):
        async with hosted_operation_lease("box-new", "home-a", journal=hosted):
            pass


@pytest.mark.asyncio
async def test_exception_releases_both_locks(hosted):
    """Break caught: an operation exception leaks a home lock and wedges future work."""
    with pytest.raises(ValueError):
        async with hosted_operation_lease("box-a", "home-a", journal=hosted):
            raise ValueError("boom")
    queue = multiprocessing.Queue()
    process = multiprocessing.Process(target=_exclusive_try, args=(str(hosted.root), "volume-home-a", queue))
    process.start(); process.join(5)
    assert queue.get(timeout=1) == "acquired"


@pytest.mark.asyncio
async def test_create_wrapper_refuses_fenced_home_before_internal_create(monkeypatch, hosted):
    """Break caught: create writes its row before discovering a migrating home."""
    from orchestrator import sandbox_manager
    monkeypatch.setattr(sandbox_manager.settings, "host_tier", "hosted")
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: hosted)
    called = False
    async def internal(*args, **kwargs):
        nonlocal called; called = True
        raise AssertionError("store/Docker create must not begin")
    monkeypatch.setattr(sandbox_manager, "_create_sandbox_unleased", internal)
    hosted.write(_record("old", "matrx-user-12345678-1234-1234-1234-123456789abc"))
    with pytest.raises(HostedOperationDenied):
        await sandbox_manager.create_sandbox("12345678-1234-1234-1234-123456789abc", "org")
    assert called is False
