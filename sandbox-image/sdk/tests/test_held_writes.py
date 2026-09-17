"""A REFUSED write is not a finished write.

Until 2026-09-17 a 400/403/409 from the cloud-files bridge ended a flush with
one WARNING and ``queue.mark_done`` — so ``replay_pending`` never returned the
event again and the user's edit was gone from the durable queue with nothing on
any surface they read. These tests drive the real watcher against a fake bridge
that refuses the way AI Dream now refuses a non-member (403
``organization_membership_required``) and prove the four things that make the
edit safe: it is still in the queue, the local file is untouched, the status
endpoint names the refusal and its remedy, and the retry is on the slow cadence
rather than a hot loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from matrx_agent.cloud_sync.downstream import PollingSubscriber
from matrx_agent.cloud_sync.queue import PersistentQueue
from matrx_agent.cloud_sync.watcher import (
    HELD_RETRY_SECONDS,
    CloudFilesWatcher,
)

REFUSAL_BODY = {
    "detail": {
        "code": "organization_membership_required",
        "message": "user 11111111-1111-1111-1111-111111111111 is not a member of organization 22222222-2222-2222-2222-222222222222",
        "remedy": "Ask an admin of that organization to add you, or open the file from an organization you belong to.",
    }
}


def _refusal(status: int = 403) -> httpx.HTTPStatusError:
    request = httpx.Request("PUT", "https://server.example/api/cloud-files/put")
    response = httpx.Response(status, json=REFUSAL_BODY, request=request)
    return httpx.HTTPStatusError("refused", request=request, response=response)


class _RefusingBridge:
    """Answers every call the way the organization-scoped bridge refuses one."""

    def __init__(self, status: int = 403):
        self.status = status
        self.put_calls: list[str] = []
        self.delete_calls: list[str] = []

    async def put_one(self, local_path: Path, remote_path: str):
        self.put_calls.append(remote_path)
        raise _refusal(self.status)

    async def delete_one(self, remote_path: str) -> None:
        self.delete_calls.append(remote_path)
        raise _refusal(self.status)

    async def close(self) -> None:  # pragma: no cover — parity with the real client
        return None


def _armed_watcher(tmp_path: Path, bridge: _RefusingBridge) -> CloudFilesWatcher:
    watcher = CloudFilesWatcher(
        cloud_root=tmp_path / "cloud-files",
        queue_path=tmp_path / "queue.jsonl",
    )
    watcher.cloud_root.mkdir(parents=True, exist_ok=True)
    watcher._client = bridge  # type: ignore[assignment]
    watcher._persistent_queue = PersistentQueue(path=tmp_path / "queue.jsonl")
    watcher._inflight_sem = asyncio.Semaphore(4)
    return watcher


def test_a_refused_upsert_stays_in_the_queue_and_names_itself(tmp_path: Path) -> None:
    bridge = _RefusingBridge()
    watcher = _armed_watcher(tmp_path, bridge)
    local = watcher.cloud_root / "notes.md"
    local.write_text("the user's words", encoding="utf-8")

    async def run() -> None:
        watcher._loop = asyncio.get_running_loop()
        event = watcher._persistent_queue.enqueue("upsert", "notes.md")
        await watcher._flush_upsert("notes.md", event.event_id)

    asyncio.run(run())

    # 1. The work is still in the durable queue — a restart replays it.
    pending = watcher._persistent_queue.replay_pending()
    assert [p.rel_path for p in pending] == ["notes.md"]
    assert pending[0].kind == "upsert"

    # 2. The user's file was never touched.
    assert local.read_text(encoding="utf-8") == "the user's words"

    # 3. A person can see WHY and WHAT TO DO.
    status = watcher.get_status()["held_writes"]
    assert status["count"] == 1
    entry = status["entries"][0]
    assert entry["rel_path"] == "notes.md"
    assert entry["status"] == 403
    assert entry["code"] == "organization_membership_required"
    assert "not a member" in entry["message"]
    assert "admin of that organization" in entry["remedy"]
    assert "organization_membership_required" in status["last_refusal"]["sentence"]
    assert watcher.get_stats()["held_writes"] == 1

    # 4. The hot loop did not spin: a non-retryable refusal is asked exactly
    #    once, and the next attempt is on the slow cadence.
    assert bridge.put_calls == ["notes.md"]
    assert entry["attempts"] == 1
    assert entry["next_attempt_ts"] - entry["last_attempt_ts"] >= HELD_RETRY_SECONDS * 0.5


def test_a_refused_delete_is_held_too(tmp_path: Path) -> None:
    bridge = _RefusingBridge(status=409)
    watcher = _armed_watcher(tmp_path, bridge)

    async def run() -> None:
        watcher._loop = asyncio.get_running_loop()
        event = watcher._persistent_queue.enqueue("delete", "gone.md")
        await watcher._flush_delete("gone.md", event.event_id)

    asyncio.run(run())

    assert [p.rel_path for p in watcher._persistent_queue.replay_pending()] == ["gone.md"]
    held = watcher.get_status()["held_writes"]
    assert held["count"] == 1 and held["entries"][0]["status"] == 409
    assert bridge.delete_calls == ["gone.md"]


def test_a_transient_failure_is_held_after_its_retries_not_dropped(
    tmp_path: Path,
) -> None:
    """The same law covers an unreachable server: the hot ladder is bounded,
    and what it could not deliver is held, never marked done."""
    bridge = _RefusingBridge(status=503)
    watcher = _armed_watcher(tmp_path, bridge)
    (watcher.cloud_root / "slow.md").write_text("x", encoding="utf-8")

    async def run() -> None:
        watcher._loop = asyncio.get_running_loop()
        # Retry ladder without the real sleeps.
        import matrx_agent.cloud_sync.watcher as mod

        original = mod.RETRY_DELAYS
        mod.RETRY_DELAYS = (0.0, 0.0)
        try:
            event = watcher._persistent_queue.enqueue("upsert", "slow.md")
            await watcher._flush_upsert("slow.md", event.event_id)
        finally:
            mod.RETRY_DELAYS = original

    asyncio.run(run())

    assert [p.rel_path for p in watcher._persistent_queue.replay_pending()] == ["slow.md"]
    assert watcher.get_status()["held_writes"]["count"] == 1
    # Bounded: the transient ladder is 1 + len(RETRY_DELAYS) attempts, not a spin.
    assert len(bridge.put_calls) == 3


def test_a_success_after_a_hold_clears_it(tmp_path: Path) -> None:
    bridge = _RefusingBridge()
    watcher = _armed_watcher(tmp_path, bridge)
    local = watcher.cloud_root / "notes.md"
    local.write_text("v1", encoding="utf-8")

    class _Accepting(_RefusingBridge):
        async def put_one(self, local_path: Path, remote_path: str):
            self.put_calls.append(remote_path)
            return {"version": 1}

    async def run() -> None:
        watcher._loop = asyncio.get_running_loop()
        event = watcher._persistent_queue.enqueue("upsert", "notes.md")
        await watcher._flush_upsert("notes.md", event.event_id)
        assert watcher.get_status()["held_writes"]["count"] == 1
        watcher._client = _Accepting()  # type: ignore[assignment]
        # The held retry re-runs the SAME event id.
        held = watcher._held["notes.md"]
        await watcher._flush_upsert("notes.md", held.event_id)

    asyncio.run(run())

    assert watcher.get_status()["held_writes"]["count"] == 0
    assert watcher._persistent_queue.replay_pending() == []


def test_a_404_on_delete_is_idempotent_success_not_a_hold(tmp_path: Path) -> None:
    """Deleting a path the server no longer has IS done — the real client
    swallows 404, so nothing is held and the event retires."""

    class _AlreadyGone(_RefusingBridge):
        async def delete_one(self, remote_path: str) -> None:
            self.delete_calls.append(remote_path)
            return None  # AsyncBridgeClient.delete_one returns on 404

    bridge = _AlreadyGone()
    watcher = _armed_watcher(tmp_path, bridge)

    async def run() -> None:
        watcher._loop = asyncio.get_running_loop()
        event = watcher._persistent_queue.enqueue("delete", "already-gone.md")
        await watcher._flush_delete("already-gone.md", event.event_id)

    asyncio.run(run())

    assert watcher.get_status()["held_writes"]["count"] == 0
    assert watcher._persistent_queue.replay_pending() == []


def test_a_new_edit_supersedes_a_held_one_without_losing_work(tmp_path: Path) -> None:
    """A newer event for the same path owns it: the old hold is released and
    the queue carries exactly the newer event."""
    bridge = _RefusingBridge()
    watcher = _armed_watcher(tmp_path, bridge)
    local = watcher.cloud_root / "notes.md"
    local.write_text("v1", encoding="utf-8")

    async def run() -> None:
        watcher._loop = asyncio.get_running_loop()
        watcher._fs_queue = asyncio.Queue()
        first = watcher._persistent_queue.enqueue("upsert", "notes.md")
        await watcher._flush_upsert("notes.md", first.event_id)
        assert watcher._held["notes.md"].event_id == first.event_id
        # A second edit arrives through the real drain loop.
        local.write_text("v2", encoding="utf-8")
        watcher._fs_queue.put_nowait(("upsert", str(local), 0.0))
        drain = asyncio.create_task(watcher._drain())
        await asyncio.sleep(0.05)
        watcher._stop_requested = True
        drain.cancel()
        try:
            await drain
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    pending = watcher._persistent_queue.replay_pending()
    assert [p.rel_path for p in pending] == ["notes.md"]
    assert watcher._held == {}
    assert local.read_text(encoding="utf-8") == "v2"


# ── The down direction: a 4xx refusal is never silent ──────────────────────


def test_a_refused_change_feed_is_named_on_the_status_object() -> None:
    class _RefusedFeed:
        async def list_changes(self, since_iso: str, limit: int = 1000):
            raise _refusal(403)

    sub = PollingSubscriber(_RefusedFeed())  # type: ignore[arg-type]

    async def run() -> None:
        task = asyncio.create_task(sub._loop(lambda change: asyncio.sleep(0)))
        await asyncio.sleep(0.05)
        await sub.stop()
        task.cancel()

    asyncio.run(run())

    status = sub.status()
    assert status["consecutive_failures"] >= 1
    assert status["last_refusal"]["status"] == 403
    assert status["last_refusal"]["code"] == "organization_membership_required"
    assert "Remedy" in status["last_refusal"]["sentence"]


@pytest.mark.parametrize("status_code", [400, 403, 409, 422, 503])
def test_every_failure_shape_is_described_with_the_servers_own_words(
    status_code: int,
) -> None:
    from matrx_agent.cloud_sync.refusals import describe_bridge_failure

    described = describe_bridge_failure(_refusal(status_code))

    assert described["status"] == status_code
    assert described["code"] == "organization_membership_required"
    assert described["remedy"]
    assert described["retryable"] is (status_code >= 500)
