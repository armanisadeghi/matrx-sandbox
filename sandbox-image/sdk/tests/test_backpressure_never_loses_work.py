"""THE BACKPRESSURE CAP NEVER LOSES WORK.

``_pending`` is a WORKING SET; the durable queue on disk is the record. Until
2026-09-17 reaching ``MAX_PENDING`` popped the OLDEST pending event, cancelled
its timer and called ``mark_done`` — retiring from the durable queue an edit
whose file had never been put. That is the same "the queue loses work" shape a
refusal used to have (``test_held_writes.py``), reached instead by a burst: an
unpack, a ``git checkout``, a build writing a tree of output.

These tests push cap+N events through the real drain loop against a bridge that
never answers, and hold the three lines that make a burst safe: every event is
still returned by ``replay_pending``, none was marked done, and the working set
never grew past the cap.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from matrx_agent.cloud_sync import watcher as watcher_module
from matrx_agent.cloud_sync.queue import PersistentQueue
from matrx_agent.cloud_sync.watcher import CloudFilesWatcher

CAP = 8
BURST = CAP + 12


class _SilentBridge:
    """Never answers — every admitted flush parks on the semaphore forever."""

    def __init__(self) -> None:
        self.put_calls: list[str] = []

    async def put_one(self, local_path: Path, remote_path: str):
        self.put_calls.append(remote_path)
        await asyncio.sleep(3600)

    async def delete_one(self, remote_path: str) -> None:
        await asyncio.sleep(3600)

    async def close(self) -> None:  # pragma: no cover — parity with the client
        return None


def _armed_watcher(tmp_path: Path) -> CloudFilesWatcher:
    watcher = CloudFilesWatcher(
        cloud_root=tmp_path / "cloud-files",
        queue_path=tmp_path / "queue.jsonl",
    )
    watcher.cloud_root.mkdir(parents=True, exist_ok=True)
    watcher._client = _SilentBridge()  # type: ignore[assignment]
    watcher._persistent_queue = PersistentQueue(path=tmp_path / "queue.jsonl")
    watcher._inflight_sem = asyncio.Semaphore(4)
    return watcher


def _run_burst(watcher: CloudFilesWatcher, count: int) -> list[int]:
    """Push ``count`` distinct paths through the real drain loop.

    Returns the size of the working set observed after every event, so the test
    can assert the bound held throughout rather than only at the end.
    """
    sizes: list[int] = []

    async def run() -> None:
        watcher._loop = asyncio.get_running_loop()
        watcher._fs_queue = asyncio.Queue()
        drain = asyncio.create_task(watcher._drain())
        for index in range(count):
            rel = f"burst/file-{index:04d}.txt"
            local = watcher.cloud_root / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(f"edit {index}", encoding="utf-8")
            await watcher._fs_queue.put(("upsert", str(local), 0.0))
            await asyncio.sleep(0)
            while not watcher._fs_queue.empty():
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            sizes.append(len(watcher._pending))
        watcher._stop_requested = True
        drain.cancel()
        try:
            await drain
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    return sizes


@pytest.fixture(autouse=True)
def _small_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cap of 8 proves the same rule as 10,000 in a test that finishes."""
    monkeypatch.setattr(watcher_module, "MAX_PENDING", CAP)
    monkeypatch.setattr(watcher_module, "MAX_DEFERRED", CAP * 4)


def test_a_burst_past_the_cap_is_still_entirely_in_the_durable_queue(
    tmp_path: Path,
) -> None:
    watcher = _armed_watcher(tmp_path)

    sizes = _run_burst(watcher, BURST)

    # 1. The working set never grew past the cap — memory stays bounded.
    assert sizes, "the drain loop processed nothing"
    assert max(sizes) <= CAP, f"working set reached {max(sizes)}, cap is {CAP}"

    # 2. Every single edit is still PENDING: a restart replays all of them.
    pending = watcher._persistent_queue.replay_pending()
    assert len(pending) == BURST, (
        f"{BURST - len(pending)} edit(s) left the durable queue without their "
        "file ever being put"
    )
    assert {event.rel_path for event in pending} == {
        f"burst/file-{index:04d}.txt" for index in range(BURST)
    }

    # 3. Nothing was marked done — mark_done means "the file is in the cloud".
    assert watcher._persistent_queue.stats()["done"] == 0

    # 4. The overflow is NAMED, not silent.
    status = watcher.get_status()["backpressure_events"]
    assert status["total"] >= BURST - CAP
    assert status["working_set_cap"] == CAP
    assert status["deferred_now"] + status["awaiting_restart"] >= BURST - CAP


def test_the_old_behaviour_would_fail_this_test(tmp_path: Path) -> None:
    """Falsifiability: the guard must be able to return the defect.

    Reinstating the drop-the-oldest cap on a watcher retires events from the
    durable queue, which is exactly what the assertions above forbid.
    """
    watcher = _armed_watcher(tmp_path)

    def drop_oldest(kind: str, rel: str, event_id: str, *, delay: float) -> bool:
        if len(watcher._pending) >= watcher_module.MAX_PENDING:
            drop_rel, (handle, drop_eid) = watcher._pending.popitem(last=False)
            handle.cancel()
            watcher._safe_mark_done(drop_eid, drop_rel)
        assert watcher._loop is not None
        handle = watcher._loop.call_later(delay, lambda: None)
        watcher._pending[rel] = (handle, event_id)
        return True

    watcher._admit = drop_oldest  # type: ignore[method-assign]

    _run_burst(watcher, BURST)

    assert len(watcher._persistent_queue.replay_pending()) < BURST
    assert watcher._persistent_queue.stats()["done"] > 0


def test_deferred_events_are_admitted_once_the_working_set_drains(
    tmp_path: Path,
) -> None:
    """Deferral is not a dead end: a freed slot takes the oldest deferred edit."""
    watcher = _armed_watcher(tmp_path)

    async def run() -> None:
        watcher._loop = asyncio.get_running_loop()
        for index in range(CAP + 3):
            rel = f"file-{index}.txt"
            event = watcher._persistent_queue.enqueue("upsert", rel)
            watcher._admit("upsert", rel, event.event_id, delay=3600.0)

        assert len(watcher._pending) == CAP
        assert len(watcher._deferred) == 3

        # A flush finishing frees two slots.
        for _ in range(2):
            rel, (handle, _eid) = watcher._pending.popitem(last=False)
            handle.cancel()

        watcher._readmit_deferred()

        assert len(watcher._pending) == CAP
        assert len(watcher._deferred) == 1
        # Oldest first — nothing jumps the burst.
        assert "file-8.txt" in watcher._pending
        assert "file-9.txt" in watcher._pending
        assert watcher._persistent_queue.stats()["done"] == 0

        watcher._stop_requested = True
        for handle, _eid in list(watcher._pending.values()):
            handle.cancel()

    asyncio.run(run())


def test_a_newer_edit_supersedes_a_deferred_one_exactly_once(tmp_path: Path) -> None:
    """The deferred index follows the same supersede rule as the pending map."""
    watcher = _armed_watcher(tmp_path)

    async def run() -> None:
        watcher._loop = asyncio.get_running_loop()
        for index in range(CAP):
            event = watcher._persistent_queue.enqueue("upsert", f"hot-{index}.txt")
            watcher._admit("upsert", f"hot-{index}.txt", event.event_id, delay=3600.0)
        first = watcher._persistent_queue.enqueue("upsert", "notes.md")
        watcher._admit("upsert", "notes.md", first.event_id, delay=3600.0)
        assert "notes.md" in watcher._deferred

        watcher._fs_queue = asyncio.Queue()
        local = watcher.cloud_root / "notes.md"
        local.write_text("newer words", encoding="utf-8")
        drain = asyncio.create_task(watcher._drain())
        await watcher._fs_queue.put(("upsert", str(local), 0.0))
        while not watcher._fs_queue.empty():
            await asyncio.sleep(0)
        await asyncio.sleep(0)

        pending_paths = {e.rel_path for e in watcher._persistent_queue.replay_pending()}
        assert "notes.md" in pending_paths, "the newest edit must still be pending"
        superseded = [
            e for e in watcher._persistent_queue.replay_pending()
            if e.event_id == first.event_id
        ]
        assert not superseded, "the superseded event should be retired exactly once"

        watcher._stop_requested = True
        drain.cancel()
        try:
            await drain
        except asyncio.CancelledError:
            pass
        for handle, _eid in list(watcher._pending.values()):
            handle.cancel()

    asyncio.run(run())
