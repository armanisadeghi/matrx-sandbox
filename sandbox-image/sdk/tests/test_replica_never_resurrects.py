"""The replica never writes a cloud-deleted or moved file back by path.

Live evidence (2026-10-21, aidream Lane E defect b): a file renamed/moved with
``PATCH /files/{id}`` and then trashed came back seconds later as a SECOND live
``files.files`` row with no device and no idempotency key — this replica's
up-sync (``PUT /cloud-files/put``) writing bytes it had downloaded back BY PATH,
with no precondition, after the cloud had removed them. One variant put the
OLD path back after a move.

These tests drive the real watcher against a fake bridge with the server's own
precondition rules (SPEC-SERVER §3: ``expected_checksum`` = the sha256 the
writer believes is live at the path, or ``absent``; a mismatch is 412 naming
the row that IS there, or none):

* every write carries a precondition;
* a remote move is applied as a local move — the old path is not left behind
  and nothing is uploaded;
* a stale copy of a file the cloud trashed is refused, never re-created, and
  the local bytes are kept.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

import httpx

from matrx_agent.cloud_sync.downstream import RemoteChange
from matrx_agent.cloud_sync.queue import PersistentQueue
from matrx_agent.cloud_sync.watcher import CloudFilesWatcher


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _Bridge:
    """Path-keyed rows with ids, and the bridge's precondition rules."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}  # live rows by path
        self.puts: list[tuple[str, str]] = []

    def seed(self, path: str, data: bytes) -> dict:
        row = {"id": str(uuid.uuid4()), "file_path": path, "checksum": _sha(data), "data": data}
        self.rows[path] = row
        return row

    async def get_one(self, rel: str) -> bytes:
        if rel not in self.rows:
            raise FileNotFoundError(rel)
        return self.rows[rel]["data"]

    async def put_one(self, local_path: Path, remote_path: str, *, expected_checksum: str):
        self.puts.append((remote_path, expected_checksum))
        current = self.rows.get(remote_path)
        ok = current is None if expected_checksum == "absent" else (
            current is not None and current["checksum"] == expected_checksum
        )
        if not ok:
            request = httpx.Request("PUT", "https://server.example/cloud-files/put")
            body = {"detail": {"error": "precondition_failed", "current_checksum": current["checksum"] if current else None}}
            response = httpx.Response(412, json=body, request=request)
            raise httpx.HTTPStatusError("412", request=request, response=response)
        data = local_path.read_bytes()
        row = current or {"id": str(uuid.uuid4()), "file_path": remote_path}
        row.update(checksum=_sha(data), data=data)
        self.rows[remote_path] = row
        return {"id": row["id"], "file_path": remote_path, "version": 1}

    async def delete_one(self, remote_path: str) -> None:
        self.rows.pop(remote_path, None)

    async def close(self) -> None:
        return None


def _watcher(tmp_path: Path, bridge: _Bridge) -> CloudFilesWatcher:
    w = CloudFilesWatcher(cloud_root=tmp_path / "cloud-files", queue_path=tmp_path / "queue.jsonl")
    w.cloud_root.mkdir(parents=True, exist_ok=True)
    w._client = bridge  # type: ignore[assignment]
    w._persistent_queue = PersistentQueue(path=tmp_path / "queue.jsonl")
    w._inflight_sem = asyncio.Semaphore(4)
    return w


def _modified(row: dict) -> RemoteChange:
    return RemoteChange(kind="modified", rel_path=row["file_path"], checksum=row["checksum"], file_id=row["id"])


async def _flush(w: CloudFilesWatcher, rel: str) -> None:
    w._loop = asyncio.get_running_loop()
    event = w._persistent_queue.enqueue("upsert", rel)
    await w._flush_upsert(rel, event.event_id)


def test_a_remote_move_moves_the_local_copy_and_uploads_nothing(tmp_path: Path) -> None:
    bridge = _Bridge()
    w = _watcher(tmp_path, bridge)

    async def run() -> None:
        row = bridge.seed("Docs/a.txt", b"report v1")
        await w._apply_remote_change(_modified(row))
        # The cloud renames it (same row id, new path).
        del bridge.rows["Docs/a.txt"]
        row["file_path"] = "Docs/sub/b.txt"
        bridge.rows["Docs/sub/b.txt"] = row
        await w._apply_remote_change(_modified(row))
        # Whatever the filesystem observer then reports for the old path, the
        # replica must not put it back.
        if (w.cloud_root / "Docs/a.txt").exists():
            await _flush(w, "Docs/a.txt")

    asyncio.run(run())
    assert not (w.cloud_root / "Docs/a.txt").exists(), "the old path was left behind"
    assert (w.cloud_root / "Docs/sub/b.txt").read_bytes() == b"report v1"
    assert set(bridge.rows) == {"Docs/sub/b.txt"}, f"rows: {sorted(bridge.rows)}"


def test_a_stale_copy_of_a_trashed_file_is_never_written_back(tmp_path: Path) -> None:
    bridge = _Bridge()
    w = _watcher(tmp_path, bridge)

    async def run() -> None:
        row = bridge.seed("Docs/sub/b.txt", b"report v1")
        await w._apply_remote_change(_modified(row))
        # The cloud trashes it; this box has not polled the deletion yet, and
        # something makes it flush the path (a replayed event, a reconcile).
        del bridge.rows["Docs/sub/b.txt"]
        w._last_hash.pop("Docs/sub/b.txt", None)
        await _flush(w, "Docs/sub/b.txt")

    asyncio.run(run())
    assert bridge.rows == {}, "the trashed file came back as a new row"
    assert bridge.puts == [("Docs/sub/b.txt", _sha(b"report v1"))]
    assert (w.cloud_root / "Docs/sub/b.txt").read_bytes() == b"report v1", "local bytes were touched"
    assert w._persistent_queue.replay_pending() == [], "a removed file must not sit held forever"
    assert w.get_status()["downstream"]["cloud_removed_kept_local"] == 1


def test_every_write_carries_a_precondition(tmp_path: Path) -> None:
    bridge = _Bridge()
    w = _watcher(tmp_path, bridge)
    (w.cloud_root / "new.md").write_text("fresh", encoding="utf-8")

    async def run() -> None:
        await _flush(w, "new.md")
        (w.cloud_root / "new.md").write_text("fresh, edited", encoding="utf-8")
        await _flush(w, "new.md")

    asyncio.run(run())
    assert bridge.puts == [("new.md", "absent"), ("new.md", _sha(b"fresh"))]
    assert bridge.rows["new.md"]["data"] == b"fresh, edited"
