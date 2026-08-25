"""Forcing tests for Postgres pool publication and bounded retirement."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.store import PostgresSandboxStore


@pytest.mark.asyncio
async def test_get_pool_publishes_only_one_pool_under_concurrency(monkeypatch):
    store = PostgresSandboxStore("postgresql://user:pass@example.test/db")
    created = []

    async def create_pool():
        await asyncio.sleep(0)
        pool = object()
        created.append(pool)
        return pool

    monkeypatch.setattr(store, "_create_pool", create_pool)

    pools = await asyncio.gather(*(store._get_pool() for _ in range(8)))

    assert len(created) == 1
    assert all(pool is created[0] for pool in pools)


@pytest.mark.asyncio
async def test_discard_pool_detaches_before_waiting_for_close(monkeypatch):
    store = PostgresSandboxStore("postgresql://user:pass@example.test/db")
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class HungPool:
        terminated = False

        async def close(self):
            close_started.set()
            await allow_close.wait()

        def terminate(self):
            self.terminated = True

    failed_pool = HungPool()
    replacement_pool = object()
    store._pool = failed_pool

    async def create_pool():
        return replacement_pool

    monkeypatch.setattr(store, "_create_pool", create_pool)

    discard = asyncio.create_task(store._discard_pool(failed_pool))
    await close_started.wait()

    # Pool retirement may still be waiting on borrowers, but new traffic is
    # no longer pinned behind the dead generation.
    assert await store._get_pool() is replacement_pool

    allow_close.set()
    await discard
    assert failed_pool.terminated is False

