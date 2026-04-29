"""Crash-resilient JSONL queue for the cloud-files watcher.

Rationale: the watcher's debounce + retry state lives in memory. A SIGKILL or
container OOM loses any pending uploads, and the shutdown bulk up-sync only
catches them on a clean stop. This module persists each pending event to an
append-only JSONL file under ``~/.matrx/runtime/cloud-sync-queue.jsonl`` so a
restarted daemon can replay them.

File format (one JSON object per line):

    {"event_id": "01HX...", "kind": "upsert"|"delete", "rel_path": "...", "queued_at": 1.7e9}
    {"event_id": "01HX...", "done": true, "completed_at": 1.7e9}

Replay logic: read all lines, mark events that have a matching `done` line as
complete, and replay the rest. Compaction (rewriting only the still-pending
entries) runs when the file exceeds a size cap.

Lock-free single-writer design — only the watcher's drain loop appends. We
fsync on every append so a kill -9 still sees the lines we acknowledged.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_logger = logging.getLogger("matrx_agent.cloud_sync.queue")

DEFAULT_PATH = Path("/home/agent/.matrx/runtime/cloud-sync-queue.jsonl")
COMPACT_THRESHOLD_BYTES = 1 << 20  # 1 MiB
COMPACT_MIN_DONE_RATIO = 0.5


def _new_event_id() -> str:
    """Time-sortable random id (12 hex chars suffix)."""
    return f"{int(time.time() * 1000):013x}-{secrets.token_hex(6)}"


@dataclass
class PendingEvent:
    event_id: str
    kind: str  # "upsert" | "delete"
    rel_path: str
    queued_at: float


class PersistentQueue:
    """Append-only JSONL log of pending watcher events.

    Not async-aware on purpose — IO is small (a few hundred bytes per line)
    and we want fsync-on-write for crash safety. Callers are expected to run
    `enqueue`/`mark_done` from a single coroutine (the drain loop) so we
    serialize naturally; the lock is belt-and-braces for any future caller.
    """

    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    # ─── Append API ──────────────────────────────────────────────────────────

    def enqueue(self, kind: str, rel_path: str) -> PendingEvent:
        """Record a new pending event. Returns the assigned event id."""
        evt = PendingEvent(
            event_id=_new_event_id(),
            kind=kind,
            rel_path=rel_path,
            queued_at=time.time(),
        )
        self._append({
            "event_id": evt.event_id,
            "kind": evt.kind,
            "rel_path": evt.rel_path,
            "queued_at": evt.queued_at,
        })
        return evt

    def mark_done(self, event_id: str) -> None:
        """Mark a previously-enqueued event as complete (success OR final failure)."""
        self._append({
            "event_id": event_id,
            "done": True,
            "completed_at": time.time(),
        })

    # ─── Replay / inspection ─────────────────────────────────────────────────

    def replay_pending(self) -> list[PendingEvent]:
        """Return events that were enqueued but not marked done.

        On corruption (truncated last line, bad JSON), the bad lines are
        skipped silently — better to miss one event than to refuse to start.
        """
        if not self.path.exists():
            return []

        pending: dict[str, PendingEvent] = {}
        with self._lock, self.path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = obj.get("event_id")
                if not isinstance(eid, str):
                    continue
                if obj.get("done"):
                    pending.pop(eid, None)
                    continue
                kind = obj.get("kind")
                rel = obj.get("rel_path")
                queued_at = obj.get("queued_at")
                if kind not in ("upsert", "delete") or not isinstance(rel, str):
                    continue
                pending[eid] = PendingEvent(
                    event_id=eid,
                    kind=kind,
                    rel_path=rel,
                    queued_at=float(queued_at) if isinstance(queued_at, (int, float)) else time.time(),
                )
        return list(pending.values())

    def stats(self) -> dict:
        """Counts for the status endpoint. Cheap O(file_size) read."""
        if not self.path.exists():
            return {"enqueued": 0, "done": 0, "pending": 0, "bytes": 0}
        enq = done = 0
        with self._lock, self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("done"):
                    done += 1
                elif obj.get("kind"):
                    enq += 1
        return {
            "enqueued": enq,
            "done": done,
            "pending": max(0, enq - done),
            "bytes": self.path.stat().st_size,
        }

    def maybe_compact(self) -> bool:
        """Rewrite the file keeping only still-pending entries if it's large
        and mostly tombstones. Returns True if compaction ran.
        """
        if not self.path.exists():
            return False
        size = self.path.stat().st_size
        if size < COMPACT_THRESHOLD_BYTES:
            return False
        s = self.stats()
        total = s["enqueued"] + s["done"]
        if total == 0 or (s["done"] / total) < COMPACT_MIN_DONE_RATIO:
            return False
        return self._compact()

    # ─── Internals ───────────────────────────────────────────────────────────

    def _append(self, obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

    def _compact(self) -> bool:
        pending = self.replay_pending()
        tmp = self.path.with_suffix(".jsonl.tmp")
        try:
            with self._lock, tmp.open("w", encoding="utf-8") as f:
                for evt in pending:
                    f.write(json.dumps({
                        "event_id": evt.event_id,
                        "kind": evt.kind,
                        "rel_path": evt.rel_path,
                        "queued_at": evt.queued_at,
                    }, separators=(",", ":")) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self.path)
            _logger.info(
                "cloud-sync queue compacted: kept %d pending events", len(pending),
            )
            return True
        except OSError as e:
            _logger.warning("cloud-sync queue compaction failed: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
