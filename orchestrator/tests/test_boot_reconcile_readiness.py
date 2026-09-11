"""Durable startup serves existing rows before the expensive Docker census."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.main import (
    _cancel_boot_reconciliation,
    _reconcile_boot_state,
    _start_boot_reconciliation,
)
from orchestrator.store import InMemorySandboxStore, PostgresSandboxStore


@pytest.mark.asyncio
async def test_boot_reconcile_runs_the_full_repair_pass_for_the_given_store() -> None:
    store = object()
    docker_summary = {"reconciled": 215, "scanned": 225, "skipped": 10, "failed": 0}
    liveness_summary = {"stopped": [], "refreshed": 215}

    with (
        patch(
            "orchestrator.reconcile.reconcile_from_docker",
            new=AsyncMock(return_value=docker_summary),
        ) as docker,
        patch(
            "orchestrator.reconcile.reconcile_liveness",
            new=AsyncMock(return_value=liveness_summary),
        ) as liveness,
    ):
        await _reconcile_boot_state(store)

    docker.assert_awaited_once_with(store)
    liveness.assert_awaited_once_with(store)


@pytest.mark.asyncio
async def test_postgres_startup_starts_reconcile_without_waiting(monkeypatch) -> None:
    """Durable rows can serve while the Docker fleet census is still running."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_reconcile(store) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr("orchestrator.main._reconcile_boot_state", blocked_reconcile)
    task = await _start_boot_reconciliation(PostgresSandboxStore("postgresql://unused"))
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not task.done()
    release.set()
    await task


@pytest.mark.asyncio
async def test_memory_startup_waits_for_reconcile(monkeypatch) -> None:
    """Memory has no durable rows, so it remains synchronous at startup."""
    called = AsyncMock()
    monkeypatch.setattr("orchestrator.main._reconcile_boot_state", called)

    task = await _start_boot_reconciliation(InMemorySandboxStore())

    assert task is None
    called.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_cancels_background_boot_reconcile_immediately() -> None:
    """Shutdown must not leave a Docker census touching a closed store."""
    cancelled = asyncio.Event()

    async def blocked() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(blocked())
    await asyncio.sleep(0)
    await _cancel_boot_reconciliation(task)

    assert cancelled.is_set()
    assert task.cancelled()
