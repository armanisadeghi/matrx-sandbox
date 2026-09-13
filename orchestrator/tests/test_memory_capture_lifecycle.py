"""Forcing guards for bounded, non-blocking lifecycle memory capture."""
from __future__ import annotations

import asyncio
import threading

import pytest

from orchestrator.memory_sync import capture_memory_from_container


@pytest.mark.asyncio
async def test_lazy_docker_archive_is_consumed_off_the_event_loop():
    entered = threading.Event()
    release = threading.Event()

    class BlockingArchive:
        def __iter__(self):
            return self

        def __next__(self):
            entered.set()
            release.wait(timeout=2)
            raise StopIteration

    class Container:
        def get_archive(self, _path):
            return BlockingArchive(), {}

    task = asyncio.create_task(
        capture_memory_from_container(Container(), "user", object())
    )
    assert await asyncio.to_thread(entered.wait, 1)

    # This tick is the forcing condition: consuming docker-py's lazy iterator
    # on the event loop prevents it from running until the archive unblocks.
    ticked = False

    async def tick():
        nonlocal ticked
        await asyncio.sleep(0)
        ticked = True

    await asyncio.wait_for(tick(), timeout=0.2)
    assert ticked
    assert not task.done()
    release.set()
    assert await asyncio.wait_for(task, timeout=1) == 0

