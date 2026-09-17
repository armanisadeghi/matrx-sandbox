"""Real-time watcher for ~/cloud-files/ → AI Dream cld_files.

Observes the cloud-files directory via watchdog, debounces events per-path
for 5 seconds, then pushes through the cloud-files bridge.

Operating modes:
    - dormant   AI Dream env vars unset; nothing to do.
    - waiting   Daemon started but neither the down-marker nor the bridge
                is reachable yet. Self-healing retry loop with backoff.
    - degraded  Bridge probe succeeded but the bulk down-sync never wrote
                its marker (so we don't have a clean seed for skip-unchanged).
                Still observes + uploads; first edits may re-upload.
    - active    Marker seen, hashes seeded, observer + drain loop running.

Crash resilience: each scheduled event is appended to the cloud-sync
PersistentQueue at ~/.matrx/runtime/cloud-sync-queue.jsonl. A SIGKILL or
container restart replays unfinished events on next boot.

Lifecycle is owned by the matrx_agent FastAPI daemon's lifespan.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from matrx_agent.cloud_sync.client import AsyncBridgeClient, BridgeConfig
from matrx_agent.cloud_sync.downstream import RemoteChange, make_subscriber
from matrx_agent.cloud_sync.paths import is_system_path
from matrx_agent.cloud_sync.refusals import (
    describe_bridge_failure,
    is_retryable_status,
    refusal_sentence,
)
from matrx_agent.cloud_sync.queue import (
    DEFAULT_PATH as DEFAULT_QUEUE_PATH,
)
from matrx_agent.cloud_sync.queue import (
    PendingEvent,
    PersistentQueue,
)

_logger = logging.getLogger("matrx_agent.cloud_sync")

DEBOUNCE_SECONDS = 5.0
MAX_FILE_SIZE = 1 << 30  # 1 GiB — matches bridge per-request cap
MAX_PENDING = 10_000
MAX_INFLIGHT = 16
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
SEED_HASH_CHUNK = 1 << 20  # 1 MiB
STABILITY_WAIT_SECONDS = 0.2

# Self-healing startup
DOWN_MARKER_WAIT_SECONDS = 60.0
DOWN_MARKER_POLL_INTERVAL = 0.5
STARTUP_BACKOFFS = (60.0, 120.0, 300.0)  # capped at last value, repeats forever
PROBE_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)

# Metrics
LATENCY_HISTORY_LEN = 256
ERROR_HISTORY_LEN = 64
COMPACT_CHECK_INTERVAL = 50  # check queue compaction every N completed events

# Echo-loop guard — when we apply a remote change to local fs, watchdog will
# fire a "modified" event that we don't want to echo back. _last_hash dedup
# would catch most of these in _flush_upsert, but the LRU is belt-and-braces
# (and skips work earlier in the drain loop). Entries expire after this TTL.
RECENTLY_APPLIED_TTL_SECONDS = 30.0
RECENTLY_APPLIED_MAX = 1024

# Suffix used by the downstream apply path for its atomic write+rename
# (see _apply_remote_change: local.suffix + CLOUD_FILES_TMP_SUFFIX). The
# filesystem observer sees these scratch files appear-then-vanish and would
# otherwise enqueue an upsert+delete for each — the delete then hits the bridge
# with a path that was never a cld_files row, producing a continuous stream of
# spurious 404s. They are internal scratch, never user content: always ignore.
CLOUD_FILES_TMP_SUFFIX = ".cloud-files.tmp"

# ─── Held writes ─────────────────────────────────────────────────────────────
# A bridge call that FAILS — refused (403 organization_membership_required,
# 409, 400) or unreachable after the hot ladder — never means the user's edit
# is done. Until 2026-09-17 it did: the flush logged one warning and called
# mark_done, so replay_pending never returned the event again and the edit was
# gone from the durable queue with nothing on any surface a person reads. The
# local file was still right, so nobody could tell.
#
# Now the event stays PENDING in the queue and the path is parked HERE: retried
# on a slow cadence (never the hot loop), counted on /internal/cloud-sync-status
# as ``held_writes``, carrying the server's own status/code/message/remedy so
# the person sees WHY and WHAT TO DO. Nothing is ever deleted or overwritten
# locally; a new edit to the same path simply supersedes the held event.
HELD_RETRY_SECONDS = 300.0  # 5 minutes, jittered — a held write is not urgent
HELD_RETRY_JITTER_FRACTION = 0.2
HELD_MAX_TRACKED = 1000  # index cap; the QUEUE, not this dict, is the truth
HELD_STATUS_SAMPLE = 20  # how many held paths the status endpoint names

# ─── Backpressure ────────────────────────────────────────────────────────────
# ``_pending`` is a WORKING SET, not the record. The record is the durable
# queue on disk. Until 2026-09-17 hitting ``MAX_PENDING`` popped the OLDEST
# pending event, cancelled its timer and called ``mark_done`` — retiring from
# the durable queue an edit whose file had never been put. That is the same
# "the queue loses work" shape a refusal used to have, reached instead by a
# burst: an unpack, a checkout, a build output directory.
#
# Now the cap only stops ADMISSION. The event is written to the durable queue
# first and then either indexed here for re-admission as soon as a slot frees,
# or — if even that index is full — left for ``replay_pending`` to return at
# the next start. Either way it stays PENDING and nothing is ever marked done
# that was not put. ``_pending`` never exceeds ``MAX_PENDING`` and the deferred
# index never exceeds ``MAX_DEFERRED``, so memory stays bounded.
MAX_DEFERRED = MAX_PENDING  # index cap; the QUEUE, not this dict, is the truth
READMIT_SECONDS = 30.0  # slow cadence — a deferred event is not urgent
READMIT_JITTER_FRACTION = 0.2


def _is_ignored_scratch(path: str) -> bool:
    return path.endswith(CLOUD_FILES_TMP_SUFFIX)


def _is_retryable_bridge_error(error: Exception) -> bool:
    """One judgement, shared with the refusal description (``refusals.py``)."""
    if not isinstance(error, httpx.HTTPStatusError):
        return True
    return is_retryable_status(error.response.status_code)


def _held_retry_delay() -> float:
    """``HELD_RETRY_SECONDS`` ± jitter — 226 boxes must not retry in lockstep."""
    spread = HELD_RETRY_SECONDS * HELD_RETRY_JITTER_FRACTION
    return max(1.0, HELD_RETRY_SECONDS + random.uniform(-spread, spread))


def _readmit_delay() -> float:
    """``READMIT_SECONDS`` ± jitter — same anti-lockstep rule as a held retry."""
    spread = READMIT_SECONDS * READMIT_JITTER_FRACTION
    return max(1.0, READMIT_SECONDS + random.uniform(-spread, spread))


@dataclass
class HeldWrite:
    """One user edit AI Dream would not accept, kept instead of dropped."""

    kind: str  # "upsert" | "delete"
    rel_path: str
    event_id: str
    refusal: dict[str, Any]
    attempts: int
    first_held_ts: float
    last_attempt_ts: float
    next_attempt_ts: float

    def to_status(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rel_path": self.rel_path,
            "attempts": self.attempts,
            "first_held_ts": self.first_held_ts,
            "last_attempt_ts": self.last_attempt_ts,
            "next_attempt_ts": self.next_attempt_ts,
            "status": self.refusal.get("status"),
            "code": self.refusal.get("code"),
            "message": self.refusal.get("message"),
            "remedy": self.refusal.get("remedy"),
        }


class _Handler(FileSystemEventHandler):
    """Watchdog handler — runs in the observer thread, hands events to the asyncio loop."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.queue = queue
        self.loop = loop

    def _enqueue(self, kind: str, path: str) -> None:
        if _is_ignored_scratch(path):
            return
        try:
            self.loop.call_soon_threadsafe(
                self.queue.put_nowait, (kind, path, time.monotonic())
            )
        except RuntimeError:
            # Loop is closing — drop the event silently.
            pass

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue("upsert", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._enqueue("upsert", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._enqueue("delete", event.src_path)
            self._enqueue("upsert", event.dest_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._enqueue("delete", event.src_path)


class _Metrics:
    """Lightweight rolling stats for /internal/cloud-sync-status + session-report."""

    def __init__(self):
        self.put_count = 0
        self.delete_count = 0
        self.bytes_uploaded = 0
        self.errors_total = 0
        self.last_success_ts: Optional[float] = None
        self.last_error_ts: Optional[float] = None
        self.last_error_message: Optional[str] = None
        self.recent_latencies_ms: deque[float] = deque(maxlen=LATENCY_HISTORY_LEN)
        self.recent_errors: deque[dict] = deque(maxlen=ERROR_HISTORY_LEN)

    def record_put(self, bytes_n: int, latency_ms: float) -> None:
        self.put_count += 1
        self.bytes_uploaded += max(0, int(bytes_n))
        self.recent_latencies_ms.append(latency_ms)
        self.last_success_ts = time.time()

    def record_delete(self, latency_ms: float) -> None:
        self.delete_count += 1
        self.recent_latencies_ms.append(latency_ms)
        self.last_success_ts = time.time()

    def record_error(self, kind: str, rel: str, error: str) -> None:
        self.errors_total += 1
        self.last_error_ts = time.time()
        self.last_error_message = error[:200]
        self.recent_errors.appendleft(
            {
                "kind": kind,
                "rel_path": rel,
                "error": error[:200],
                "ts": time.time(),
            }
        )

    def percentile(self, p: float) -> Optional[int]:
        n = len(self.recent_latencies_ms)
        if n == 0:
            return None
        sorted_vals = sorted(self.recent_latencies_ms)
        idx = max(0, min(n - 1, int(p * n)))
        return int(sorted_vals[idx])


class CloudFilesWatcher:
    """In-process watcher that pushes ~/cloud-files/ changes to AI Dream in real time."""

    def __init__(
        self,
        cloud_root: Path = Path("/home/agent/cloud-files"),
        marker_path: Path = Path(
            "/home/agent/.matrx/runtime/cloud-files-down-complete"
        ),
        queue_path: Optional[Path] = None,
    ):
        self.cloud_root = cloud_root
        self.marker_path = marker_path
        self._queue_path = queue_path
        self._cfg: Optional[BridgeConfig] = None
        self._client: Optional[AsyncBridgeClient] = None
        self._observer: Optional[Observer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._fs_queue: Optional[asyncio.Queue] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._startup_task: Optional[asyncio.Task] = None
        # Maps rel_path → (TimerHandle, event_id) — at most one pending timer per path.
        self._pending: "OrderedDict[str, tuple[asyncio.TimerHandle, str]]" = (
            OrderedDict()
        )
        self._inflight_sem: Optional[asyncio.Semaphore] = None
        self._last_hash: dict[str, str] = {}
        self._event_arrivals: dict[str, float] = {}
        self._mode: str = (
            "init"  # init|dormant|waiting|degraded|active|stopping|stopped
        )
        self._mode_since: float = time.time()
        self._stop_requested = False
        self._persistent_queue: Optional[PersistentQueue] = None
        self._completed_since_compact = 0
        self._metrics = _Metrics()
        self._started_at: Optional[float] = None
        # Downstream (cloud → sandbox) subscriber, set up in _activate.
        self._subscriber: Any = None
        # Echo-loop guard: rel_path → (sha256, expires_at_monotonic).
        # Populated when _apply_remote_change writes to disk; consulted in
        # the drain loop to suppress the watchdog event that local FS write
        # triggers. Entries are evicted lazily on lookup.
        self._recently_applied: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        # Counts for metrics — number of remote changes received vs applied.
        self._remote_received = 0
        self._remote_applied = 0
        self._remote_echo_suppressed = 0
        # Writes AI Dream refused, still PENDING in the durable queue.
        self._held: "OrderedDict[str, HeldWrite]" = OrderedDict()
        self._held_total = 0
        # Events written to the durable queue but NOT admitted to the working
        # set because it was full. rel_path → (kind, event_id). Never marked
        # done; re-admitted as soon as a slot frees.
        self._deferred: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
        self._backpressure_events = 0
        self._backpressure_unindexed = 0
        self._readmit_handle: Optional[asyncio.TimerHandle] = None

    @property
    def mode(self) -> str:
        return self._mode

    def _set_mode(self, mode: str) -> None:
        if mode != self._mode:
            _logger.info("cloud-files: mode %s → %s", self._mode, mode)
            self._mode = mode
            self._mode_since = time.time()

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Top-level entry point. Always non-blocking from the lifespan's POV
        because we either return immediately (dormant) or kick off a background
        retry loop. The actual observer is started by ``_activate``.
        """
        self._cfg = BridgeConfig.from_env()
        if self._cfg is None:
            self._set_mode("dormant")
            from matrx_agent.bridge_headers import published_identity_failure

            failure = published_identity_failure()
            if failure:
                # Not an unwired image — the boot-time identity writer failed
                # and the entrypoint kept going. Never dormant-and-quiet.
                _logger.error(
                    "cloud-files: this sandbox published NO identity because "
                    "write-bridge-env.sh failed, so cloud-files sync is off on "
                    "a box that was meant to sync. %s",
                    failure,
                )
            else:
                _logger.info(
                    "cloud-files: AI Dream env not configured — watcher dormant"
                )
            return

        if not self.cloud_root.exists():
            # ensure-layout.sh creates this on every boot, but be defensive.
            try:
                self.cloud_root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                _logger.warning("cloud-files: cannot create %s: %s", self.cloud_root, e)
                self._set_mode("dormant")
                return

        self._loop = asyncio.get_running_loop()
        # Run the rest of startup as a background task so the lifespan returns
        # even while we wait for the down-marker / probe the bridge.
        self._startup_task = asyncio.create_task(self._self_heal_loop())

    async def stop(self) -> None:
        if self._mode in ("init", "dormant", "stopped"):
            self._set_mode("stopped")
            return
        self._stop_requested = True
        self._set_mode("stopping")

        if self._startup_task is not None and not self._startup_task.done():
            self._startup_task.cancel()
            try:
                await self._startup_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._startup_task = None

        if self._subscriber is not None:
            try:
                await self._subscriber.stop()
            except Exception as e:  # noqa: BLE001
                _logger.warning("cloud-files: subscriber stop error: %s", e)
            self._subscriber = None

        if self._readmit_handle is not None:
            self._readmit_handle.cancel()
            self._readmit_handle = None
        for handle, _eid in list(self._pending.values()):
            handle.cancel()
        self._pending.clear()
        # Deferred events are NOT cleared out of the queue — they were never
        # marked done, so replay_pending returns them on the next start.
        self._deferred.clear()

        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception as e:  # noqa: BLE001
                _logger.warning("cloud-files: observer stop error: %s", e)
            self._observer = None

        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

        if self._client is not None:
            try:
                await self._client.close()
            except Exception as e:  # noqa: BLE001
                _logger.warning("cloud-files: error closing http client: %s", e)
            self._client = None

        self._set_mode("stopped")
        _logger.info("cloud-files: watcher stopped")

    # ─── Self-healing startup loop ───────────────────────────────────────────

    async def _self_heal_loop(self) -> None:
        """Keep trying until we either reach `active` or `degraded`, then return.

        Loops on: down-marker (60s) → bridge probe → exponential backoff sleep.
        Exits cleanly if `stop()` is called concurrently.
        """
        attempt = 0
        try:
            while not self._stop_requested:
                self._set_mode("waiting")
                if await self._await_down_marker():
                    await self._activate(degraded=False)
                    return
                if await self._probe_bridge():
                    _logger.warning(
                        "cloud-files: down-marker missing but bridge reachable — "
                        "starting in DEGRADED mode (no seed; first edits may re-upload)",
                    )
                    await self._activate(degraded=True)
                    return
                # Neither reachable — back off and try again.
                delay = STARTUP_BACKOFFS[min(attempt, len(STARTUP_BACKOFFS) - 1)]
                _logger.warning(
                    "cloud-files: marker absent + bridge unreachable; "
                    "retrying in %.0fs (attempt %d)",
                    delay,
                    attempt + 1,
                )
                attempt += 1
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            _logger.exception("cloud-files: self-heal loop crashed: %s", e)
            self._set_mode("dormant")

    async def _await_down_marker(self) -> bool:
        """Wait up to DOWN_MARKER_WAIT_SECONDS for the bulk down-sync to finish."""
        deadline = asyncio.get_running_loop().time() + DOWN_MARKER_WAIT_SECONDS
        while True:
            if self._stop_requested:
                return False
            if self.marker_path.exists():
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(DOWN_MARKER_POLL_INTERVAL)

    async def _probe_bridge(self) -> bool:
        """Hit the bridge's public health endpoint. Returns True on 2xx."""
        if self._cfg is None:
            return False
        url = f"{self._cfg.url}/api/cloud-files/integrations.aidream"
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
                r = await c.get(url)
                return r.status_code < 400
        except Exception as e:  # noqa: BLE001
            _logger.debug("cloud-files: bridge probe failed: %s", e)
            return False

    async def _activate(self, *, degraded: bool) -> None:
        """Set up the observer, queue replay, and drain loop. Called once per process."""
        assert self._loop is not None and self._cfg is not None

        self._fs_queue = asyncio.Queue()
        self._inflight_sem = asyncio.Semaphore(MAX_INFLIGHT)
        self._client = AsyncBridgeClient(self._cfg)
        self._persistent_queue = PersistentQueue(
            path=self._queue_path or DEFAULT_QUEUE_PATH,
        )
        self._started_at = time.time()

        if not degraded:
            try:
                # Hashing a persistent cloud-files tree can take minutes and
                # performs blocking disk reads. Never run that work on the
                # FastAPI event loop: it would stall PTY handshakes and every
                # filesystem/exec route in the sandbox daemon.
                await asyncio.to_thread(self._seed_hashes)
            except Exception as e:  # noqa: BLE001
                _logger.warning("cloud-files: seed walk failed (continuing): %s", e)
            # Reconcile against cld_files: files in the persistent volume but
            # NOT in the user's cld_files (or with a different checksum) need
            # to be uploaded. Without this, files that pre-existed in the
            # volume from a prior sandbox boot — but were never actually
            # uploaded — stay invisible to the user forever.
            try:
                queued = await self._reconcile_against_remote()
                if queued:
                    _logger.info(
                        "cloud-files: reconcile queued %d file(s) missing-or-changed in cld_files",
                        queued,
                    )
            except Exception as e:  # noqa: BLE001
                _logger.warning(
                    "cloud-files: reconcile against cld_files failed: %s", e
                )

        self._observer = Observer()
        self._observer.schedule(
            _Handler(self._fs_queue, self._loop),
            str(self.cloud_root),
            recursive=True,
        )
        self._observer.start()

        self._drain_task = asyncio.create_task(self._drain())

        # Replay persisted events from a previous (possibly crashed) run.
        try:
            replayed = self._persistent_queue.replay_pending()
            if replayed:
                self._enqueue_replay(replayed)
                _logger.info(
                    "cloud-files: replayed %d pending events from %s",
                    len(replayed),
                    self._persistent_queue.path,
                )
        except Exception as e:  # noqa: BLE001
            _logger.warning("cloud-files: queue replay failed: %s", e)

        # B1 — start the one down-direction subscriber (the bridge change feed).
        try:
            self._subscriber = make_subscriber(self._client, self._cfg)
            await self._subscriber.start(self._apply_remote_change)
        except Exception as e:  # noqa: BLE001
            _logger.warning("cloud-files: downstream subscriber failed to start: %s", e)
            self._subscriber = None

        self._set_mode("degraded" if degraded else "active")
        _logger.info(
            "cloud-files: watcher %s (root=%s, %d seeded hashes, debounce=%.1fs)",
            self._mode,
            self.cloud_root,
            len(self._last_hash),
            DEBOUNCE_SECONDS,
        )

    def _enqueue_replay(self, events: list[PendingEvent]) -> None:
        """Schedule replay events with zero debounce (already aged)."""
        assert self._loop is not None
        for evt in events:
            if is_system_path(evt.rel_path):
                self._safe_mark_done(evt.event_id)
                continue
            # Replays don't go through _persistent_queue.enqueue again — they're
            # already on disk. They go through the SAME admission gate, so a
            # queue holding more than MAX_PENDING events cannot blow the working
            # set on start: the surplus is deferred, still PENDING, and admitted
            # as slots free.
            self._admit(evt.kind, evt.rel_path, evt.event_id, delay=0.0)
            self._event_arrivals.setdefault(evt.rel_path, time.monotonic())

    # ─── Setup helpers ───────────────────────────────────────────────────────

    def _seed_hashes(self) -> None:
        """Walk cloud_root once, populate last_hash so we don't re-upload existing files."""
        for p in self.cloud_root.rglob("*"):
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                rel = self._rel_path(p)
                if rel is None:
                    continue
                if self._is_dotpath(rel):
                    continue
                if is_system_path(rel):
                    continue
                if _is_ignored_scratch(rel):
                    continue
                if p.stat().st_size > MAX_FILE_SIZE:
                    continue
                self._last_hash[rel] = self._sha256(p)
            except OSError:
                continue

    async def _reconcile_against_remote(self) -> int:
        """One-shot startup reconciliation against cld_files.

        The seed walk above tells us what's on disk. That's only HALF the
        truth — the persistent volume can carry files from a previous
        sandbox boot that were never actually uploaded to AI Dream. Without
        this reconcile, those files look "already synced" forever (the
        watcher's last-hash matches; no event ever fires; the user never
        sees them in the AI Dream Files panel).

        Strategy: ask the bridge for its current cld_files snapshot, build
        a {file_path: checksum} map, and for each local file:
          - If absent from cld_files → queue an upload.
          - If present but checksum differs → queue an upload.
          - If present with matching checksum → nothing to do.

        Best-effort. Network failures, missing checksum fields, etc.
        downgrade to "no reconcile this boot" — the next user edit will
        eventually trigger a sync. Returns the count of files queued.
        """
        if self._client is None or self._loop is None:
            return 0
        try:
            remote_files = await self._client.list_files()
        except Exception as e:  # noqa: BLE001
            _logger.warning("cloud-files: list_files for reconcile failed: %s", e)
            return 0

        remote_index: dict[str, str] = {}
        for row in remote_files or []:
            rp = row.get("file_path") or row.get("path")
            ck = row.get("checksum") or row.get("sha256")
            if rp:
                remote_index[str(rp)] = str(ck) if ck else ""

        queued = 0
        for rel, local_hash in list(self._last_hash.items()):
            remote_hash = remote_index.get(rel)
            if remote_hash and remote_hash == local_hash:
                # In sync — leave alone.
                continue
            # Either missing from remote or checksum mismatch — schedule an upload.
            # Use the existing debounce machinery so we don't thunder on boot;
            # the 5s window batches naturally. Queue-event shape matches
            # _Handler._enqueue: (kind, abs_path, t_arrival).
            self._fs_queue.put_nowait(  # type: ignore[union-attr]
                ("upsert", str(self.cloud_root / rel), time.monotonic())
            )
            queued += 1
        return queued

    # ─── Path helpers ────────────────────────────────────────────────────────

    def _rel_path(self, abs_path: Path) -> Optional[str]:
        """Resolve abs_path and return its path relative to cloud_root, or None if outside."""
        try:
            resolved = abs_path.resolve()
            root = self.cloud_root.resolve()
            return str(resolved.relative_to(root))
        except (ValueError, OSError):
            return None

    @staticmethod
    def _is_dotpath(rel: str) -> bool:
        return any(part.startswith(".") for part in rel.split("/"))

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(SEED_HASH_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()

    # ─── Event loop ──────────────────────────────────────────────────────────

    async def _drain(self) -> None:
        assert self._fs_queue is not None and self._loop is not None
        while not self._stop_requested:
            try:
                kind, abs_path, t_arrival = await self._fs_queue.get()
            except asyncio.CancelledError:
                return

            try:
                # Free slots reached by the last flush belong to work that was
                # deferred before this event existed.
                self._readmit_deferred()

                p = Path(abs_path)
                rel = self._rel_path(p)
                if rel is None:
                    _logger.debug("cloud-files: ignoring %s (outside root)", abs_path)
                    continue
                if self._is_dotpath(rel):
                    continue
                if is_system_path(rel):
                    continue
                if _is_ignored_scratch(rel):
                    continue

                # Echo-loop guard. If we just applied a remote change to this
                # path AND the local bytes still match what we wrote, watchdog
                # is just echoing our own write back at us — drop it cheaply
                # before it eats a debounce slot. The hash short-circuit in
                # _flush_upsert is the second line of defence.
                if self._is_recent_apply_echo(rel):
                    self._remote_echo_suppressed += 1
                    continue

                # Cancel any pending timer + reuse its event_id is wrong (the
                # old event might already be in flight). Always allocate a new
                # event_id; the in-memory pending dict ensures we only fire one.
                old = self._pending.pop(rel, None)
                if old is not None:
                    old[0].cancel()
                    # Mark the superseded event done so the JSONL doesn't grow
                    # forever. A newer edit to the same path OWNS it now, so any
                    # hold parked on it is released with it — the new event is
                    # the one that will be accepted or held.
                    self._safe_mark_done(old[1], rel)

                # A newer edit to this path also supersedes any DEFERRED event
                # for it — same rule as the pending one above.
                stale = self._deferred.pop(rel, None)
                if stale is not None:
                    self._safe_mark_done(stale[1], rel)

                self._event_arrivals.setdefault(rel, t_arrival)

                # Persist FIRST — the durable queue is the record, and it holds
                # the event whether or not the working set has room for it.
                evt = (
                    self._persistent_queue.enqueue(kind, rel)
                    if self._persistent_queue
                    else None
                )
                event_id = evt.event_id if evt else f"mem-{id(rel):x}"

                self._admit(kind, rel, event_id, delay=DEBOUNCE_SECONDS)
            except Exception as e:  # noqa: BLE001
                _logger.exception("cloud-files: drain error: %s", e)

    # ─── Admission (the cap stops admission, never the work) ─────────────────

    def _admit(self, kind: str, rel: str, event_id: str, *, delay: float) -> bool:
        """Put one already-persisted event into the working set, if there is room.

        Returns True when a timer was scheduled. When the working set is full
        the event is DEFERRED, never dropped and never marked done: its file
        was not put, so it is not finished.
        """
        if self._loop is None:
            return False
        if len(self._pending) >= MAX_PENDING and rel not in self._pending:
            self._defer(kind, rel, event_id)
            return False
        handle = self._loop.call_later(
            delay,
            lambda r=rel, eid=event_id, k=kind: asyncio.create_task(
                self._flush_upsert(r, eid) if k == "upsert" else self._flush_delete(r, eid)
            ),
        )
        self._pending[rel] = (handle, event_id)
        return True

    def _defer(self, kind: str, rel: str, event_id: str) -> None:
        """Hold an event OUT of the working set — it stays PENDING on disk."""
        self._backpressure_events += 1
        if len(self._deferred) >= MAX_DEFERRED:
            self._backpressure_unindexed += 1
            _logger.warning(
                "cloud-files: working set (%d) and deferred index (%d) are both "
                "full — %s %s stays PENDING in %s and is replayed at the next "
                "start. Nothing is dropped; this box is behind.",
                MAX_PENDING,
                MAX_DEFERRED,
                kind.upper(),
                rel,
                self._persistent_queue.path if self._persistent_queue else "the queue",
            )
        else:
            self._deferred[rel] = (kind, event_id)
            if len(self._deferred) == 1:
                _logger.warning(
                    "cloud-files: working set full (%d) — %s %s is deferred, not "
                    "dropped. It stays PENDING in the sync queue and is admitted "
                    "as soon as a slot frees; counted as backpressure_events on "
                    "/internal/cloud-sync-status.",
                    MAX_PENDING,
                    kind.upper(),
                    rel,
                )
        self._schedule_readmit()

    def _schedule_readmit(self) -> None:
        """One slow-cadence timer, so deferral converges with nobody watching."""
        if self._loop is None or self._stop_requested or self._readmit_handle is not None:
            return
        self._readmit_handle = self._loop.call_later(
            _readmit_delay(), self._on_readmit_timer
        )

    def _on_readmit_timer(self) -> None:
        self._readmit_handle = None
        self._readmit_deferred()

    def _readmit_deferred(self) -> None:
        """Move deferred events into the working set while there is room."""
        if not self._deferred or self._loop is None or self._stop_requested:
            return
        while self._deferred and len(self._pending) < MAX_PENDING:
            rel, (kind, event_id) = self._deferred.popitem(last=False)
            if rel in self._pending:
                # A newer edit to the same path owns it now — that event is the
                # one that will be put, so this one is genuinely superseded.
                self._safe_mark_done(event_id, rel)
                continue
            # Already aged in the queue; no second debounce.
            self._admit(kind, rel, event_id, delay=0.0)
        if self._deferred:
            self._schedule_readmit()

    def _safe_mark_done(self, event_id: str, rel: Optional[str] = None) -> None:
        """Retire an event. ``rel`` also clears any hold parked on that path.

        Only a genuinely FINISHED event comes here: accepted by the bridge,
        superseded by a newer edit, or nothing to send (the file is gone, it is
        a system path, it is over the size cap). A REFUSAL is not finished —
        that goes to :meth:`_hold`.
        """
        if rel is not None:
            self._release_hold(rel)
        if not self._persistent_queue or event_id.startswith("mem-"):
            return
        try:
            self._persistent_queue.mark_done(event_id)
            self._completed_since_compact += 1
            if self._completed_since_compact >= COMPACT_CHECK_INTERVAL:
                self._completed_since_compact = 0
                self._persistent_queue.maybe_compact()
        except Exception as e:  # noqa: BLE001
            _logger.warning("cloud-files: mark_done failed: %s", e)

    # ─── Held writes (a refusal is not a completion) ─────────────────────────

    def _hold(self, kind: str, rel: str, event_id: str, error: BaseException) -> None:
        """Park a write AI Dream would not accept — never mark it done.

        The event stays PENDING in the persistent queue, so a restart replays
        it; the path is parked here so the daemon's status endpoint can name
        the refusal; and a slow-cadence retry is scheduled so a transient
        server state (a membership that is being fixed, a 503 while the lookup
        is unavailable) converges without anybody doing anything.
        """
        described = describe_bridge_failure(error)
        now = time.time()
        previous = self._held.get(rel)
        attempts = (previous.attempts + 1) if previous else 1
        delay = _held_retry_delay()
        held = HeldWrite(
            kind=kind,
            rel_path=rel,
            event_id=event_id,
            refusal=described,
            attempts=attempts,
            first_held_ts=previous.first_held_ts if previous else now,
            last_attempt_ts=now,
            next_attempt_ts=now + delay,
        )
        if rel in self._held:
            self._held.pop(rel, None)
        elif len(self._held) >= HELD_MAX_TRACKED:
            # The index is a view, not the truth: the queue still holds every
            # event. Say so rather than silently forgetting the oldest entry.
            dropped, _ = self._held.popitem(last=False)
            _logger.warning(
                "cloud-files: held-write index is full (%d) — %s is no longer "
                "named on the status endpoint, but its event is still PENDING "
                "in %s and will be retried.",
                HELD_MAX_TRACKED,
                dropped,
                self._persistent_queue.path if self._persistent_queue else "the queue",
            )
        self._held[rel] = held
        self._held_total += 1
        self._metrics.record_error(kind, rel, described["error"])
        self._event_arrivals.pop(rel, None)
        _logger.error(
            "cloud-files: %s %s was NOT accepted by AI Dream (%s). The edit is "
            "HELD, not lost: your file on disk is untouched, the event stays in "
            "the sync queue, and this box will try again in about %.0fs "
            "(attempt %d). It is counted as held_writes on "
            "/internal/cloud-sync-status.",
            kind.upper(),
            rel,
            refusal_sentence(described),
            delay,
            attempts,
        )
        self._schedule_held_retry(held, delay)

    def _schedule_held_retry(self, held: HeldWrite, delay: float) -> None:
        """Re-run the flush on the SLOW cadence — never the hot loop."""
        if self._loop is None or self._stop_requested:
            return
        kind, rel, eid = held.kind, held.rel_path, held.event_id
        # A held retry takes a working-set slot like anything else: at the cap it
        # waits another slow cycle rather than growing the map. The event stays
        # PENDING in the queue either way, and the retry stays off the hot loop.
        if len(self._pending) >= MAX_PENDING and rel not in self._pending:
            held.next_attempt_ts = time.time() + delay
            self._loop.call_later(
                delay,
                lambda h=held: self._schedule_held_retry(h, _held_retry_delay()),
            )
            return
        handle = self._loop.call_later(
            delay,
            lambda r=rel, e=eid, k=kind: asyncio.create_task(
                self._flush_upsert(r, e) if k == "upsert" else self._flush_delete(r, e)
            ),
        )
        self._pending[rel] = (handle, eid)

    def _release_hold(self, rel: str) -> None:
        """Drop a held write once its path has been accepted or superseded."""
        if self._held.pop(rel, None) is not None:
            _logger.info("cloud-files: held write for %s cleared", rel)

    def _held_status(self) -> dict[str, Any]:
        entries = [h.to_status() for h in list(self._held.values())[:HELD_STATUS_SAMPLE]]
        last = max(self._held.values(), key=lambda h: h.last_attempt_ts, default=None)
        return {
            "count": len(self._held),
            "held_total": self._held_total,
            "truncated": max(0, len(self._held) - len(entries)),
            "entries": entries,
            "last_refusal": (
                {**last.to_status(), "sentence": refusal_sentence(last.refusal)}
                if last is not None
                else None
            ),
        }

    # ─── Flush handlers ──────────────────────────────────────────────────────

    async def _flush_upsert(self, rel: str, event_id: str) -> None:
        if is_system_path(rel):
            self._event_arrivals.pop(rel, None)
            self._safe_mark_done(event_id, rel)
            return
        # Remove from pending so a new event can schedule a replacement.
        prev = self._pending.pop(rel, None)
        # If a different event_id is now pending for this path, the newer one
        # supersedes us — let it handle the upload.
        if prev is not None and prev[1] != event_id:
            self._pending[rel] = prev  # restore the newer one
            self._safe_mark_done(event_id, rel)
            return

        assert self._inflight_sem is not None and self._client is not None
        async with self._inflight_sem:
            local = self.cloud_root / rel
            try:
                if local.is_symlink():
                    _logger.info(
                        "cloud-files: skipping %s (symlink, v1 limitation)",
                        rel,
                    )
                    self._event_arrivals.pop(rel, None)
                    self._safe_mark_done(event_id, rel)
                    return
                if not local.exists():
                    # Created-then-deleted within the debounce window. Not an error.
                    self._event_arrivals.pop(rel, None)
                    self._safe_mark_done(event_id, rel)
                    return
                size = local.stat().st_size
                if size > MAX_FILE_SIZE:
                    _logger.info(
                        "cloud-files: skipping %s (>1 GiB cap, size=%d)",
                        rel,
                        size,
                    )
                    self._event_arrivals.pop(rel, None)
                    self._safe_mark_done(event_id, rel)
                    return

                # Stability check: re-stat after a short wait. If size is still moving,
                # the agent is mid-write — re-queue with a fresh debounce.
                await asyncio.sleep(STABILITY_WAIT_SECONDS)
                if not local.exists():
                    self._event_arrivals.pop(rel, None)
                    self._safe_mark_done(event_id, rel)
                    return
                if local.stat().st_size != size:
                    if self._loop is not None:
                        new_evt = (
                            self._persistent_queue.enqueue("upsert", rel)
                            if self._persistent_queue
                            else None
                        )
                        new_eid = new_evt.event_id if new_evt else f"mem-{id(rel):x}"
                        handle = self._loop.call_later(
                            DEBOUNCE_SECONDS,
                            lambda r=rel, eid=new_eid: asyncio.create_task(
                                self._flush_upsert(r, eid)
                            ),
                        )
                        self._pending[rel] = (handle, new_eid)
                    self._safe_mark_done(event_id, rel)
                    return

                # Pre-send hash short-circuit (A5: don't even hit the bridge if
                # local content matches what we last uploaded).
                # Files may be as large as the 1 GiB sync cap. Hashing them on
                # the event loop makes unrelated PTY/WebSocket traffic appear
                # to connect and then die under load.
                new_hash = await asyncio.to_thread(self._sha256, local)
                if self._last_hash.get(rel) == new_hash:
                    self._event_arrivals.pop(rel, None)
                    self._safe_mark_done(event_id, rel)
                    return

                t0 = self._event_arrivals.get(rel, time.monotonic())
                last_err: Optional[Exception] = None
                for delay in (0.0, *RETRY_DELAYS):
                    if delay > 0:
                        await asyncio.sleep(delay)
                    try:
                        result = await self._client.put_one(local, rel)
                        latency_ms = (time.monotonic() - t0) * 1000
                        is_new = isinstance(result, dict) and result.get("version") == 1
                        _logger.info(
                            "cloud-files: PUT %s %d bytes new=%s latency_ms=%d",
                            rel,
                            size,
                            is_new,
                            int(latency_ms),
                        )
                        self._last_hash[rel] = new_hash
                        self._metrics.record_put(size, latency_ms)
                        self._event_arrivals.pop(rel, None)
                        self._safe_mark_done(event_id, rel)
                        return
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                        if not _is_retryable_bridge_error(e):
                            break
                self._hold("upsert", rel, event_id, last_err or RuntimeError("unknown"))
            except Exception as e:  # noqa: BLE001
                _logger.exception("cloud-files: flush_upsert error for %s: %s", rel, e)
                self._hold("upsert", rel, event_id, e)

    async def _flush_delete(self, rel: str, event_id: str) -> None:
        if is_system_path(rel):
            self._event_arrivals.pop(rel, None)
            self._safe_mark_done(event_id, rel)
            return
        prev = self._pending.pop(rel, None)
        if prev is not None and prev[1] != event_id:
            self._pending[rel] = prev
            self._safe_mark_done(event_id, rel)
            return

        assert self._inflight_sem is not None and self._client is not None
        async with self._inflight_sem:
            local = self.cloud_root / rel
            try:
                # If the path came back to life inside the debounce window, treat
                # it as an upsert instead of a delete.
                if local.exists() and not local.is_symlink():
                    if self._loop is not None:
                        new_evt = (
                            self._persistent_queue.enqueue("upsert", rel)
                            if self._persistent_queue
                            else None
                        )
                        new_eid = new_evt.event_id if new_evt else f"mem-{id(rel):x}"
                        handle = self._loop.call_later(
                            0.0,
                            lambda r=rel, eid=new_eid: asyncio.create_task(
                                self._flush_upsert(r, eid)
                            ),
                        )
                        self._pending[rel] = (handle, new_eid)
                    self._safe_mark_done(event_id, rel)
                    return

                t0 = self._event_arrivals.get(rel, time.monotonic())
                last_err: Optional[Exception] = None
                for delay in (0.0, *RETRY_DELAYS):
                    if delay > 0:
                        await asyncio.sleep(delay)
                    try:
                        await self._client.delete_one(rel)
                        latency_ms = (time.monotonic() - t0) * 1000
                        _logger.info(
                            "cloud-files: DELETE %s latency_ms=%d",
                            rel,
                            int(latency_ms),
                        )
                        self._last_hash.pop(rel, None)
                        self._metrics.record_delete(latency_ms)
                        self._event_arrivals.pop(rel, None)
                        self._safe_mark_done(event_id, rel)
                        return
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                        if not _is_retryable_bridge_error(e):
                            break
                self._hold("delete", rel, event_id, last_err or RuntimeError("unknown"))
            except Exception as e:  # noqa: BLE001
                _logger.exception("cloud-files: flush_delete error for %s: %s", rel, e)
                self._hold("delete", rel, event_id, e)

    # ─── Downstream / echo-loop helpers ──────────────────────────────────────

    def _is_recent_apply_echo(self, rel: str) -> bool:
        """Return True if rel was applied from a remote change recently and
        the file's current hash still matches the bytes we wrote.
        """
        entry = self._recently_applied.get(rel)
        if entry is None:
            return False
        cached_hash, expires_at = entry
        if time.monotonic() > expires_at:
            self._recently_applied.pop(rel, None)
            return False
        local = self.cloud_root / rel
        if not local.exists() or local.is_symlink():
            return False
        try:
            return self._sha256(local) == cached_hash
        except OSError:
            return False

    def _remember_apply(self, rel: str, content_hash: str) -> None:
        """Record that we just wrote rel with the given content hash so
        watchdog's echo of that write can be suppressed.
        """
        if len(self._recently_applied) >= RECENTLY_APPLIED_MAX:
            self._recently_applied.popitem(last=False)
        self._recently_applied[rel] = (
            content_hash,
            time.monotonic() + RECENTLY_APPLIED_TTL_SECONDS,
        )

    async def _apply_remote_change(self, change: RemoteChange) -> None:
        """Callback wired into the downstream subscriber.

        For 'modified': fetch bytes via the bridge, write atomically, update
        _last_hash so the resulting watchdog event de-dups in _flush_upsert,
        and push the path into the recently-applied LRU.

        For 'deleted': unlink locally, drop from _last_hash.

        Failures here are logged but never raise — the next polling cycle
        will retry naturally.
        """
        self._remote_received += 1
        rel = change.rel_path or ""
        if not rel or self._is_dotpath(rel) or is_system_path(rel):
            return

        # Path safety: refuse anything that resolves outside cloud_root.
        # Build the candidate path the same way _rel_path does the inverse,
        # but resolve manually for absolute parent dirs.
        local = self.cloud_root / rel
        try:
            local_resolved = local.resolve()
            root_resolved = self.cloud_root.resolve()
            local_resolved.relative_to(root_resolved)
        except (ValueError, OSError):
            _logger.warning("cloud-files: refusing remote write outside root: %r", rel)
            return

        if change.kind == "deleted":
            try:
                if local.exists() and not local.is_symlink():
                    local.unlink()
                    self._last_hash.pop(rel, None)
                    self._remote_applied += 1
                    _logger.info("cloud-files: applied remote DELETE %s", rel)
            except OSError as e:
                _logger.warning("cloud-files: remote DELETE failed for %s: %s", rel, e)
            return

        # change.kind == "modified" → fetch + write
        if self._client is None:
            return

        # If our local hash already matches the remote checksum, skip — bytes
        # are identical (e.g. the change came from this same sandbox writing,
        # round-tripping through cld_files, and back).
        if change.checksum and self._last_hash.get(rel) == change.checksum:
            return

        try:
            getter = getattr(self._client, "get_one", None)
            if getter is None:
                _logger.warning(
                    "cloud-files: client missing get_one; skipping remote pull for %s",
                    rel,
                )
                return
            data: bytes = await getter(rel)
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001
            _logger.warning("cloud-files: remote GET failed for %s: %s", rel, e)
            return

        new_hash = hashlib.sha256(data).hexdigest()
        # Skip if local already matches what we'd write.
        try:
            if (
                local.exists()
                and not local.is_symlink()
                and await asyncio.to_thread(self._sha256, local) == new_hash
            ):
                self._last_hash[rel] = new_hash
                self._remember_apply(rel, new_hash)
                return
        except OSError:
            pass

        # Atomic write: tmp + rename.
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_suffix(local.suffix + ".cloud-files.tmp")
            tmp.write_bytes(data)
            tmp.replace(local)
        except OSError as e:
            _logger.warning("cloud-files: failed to write %s: %s", rel, e)
            return

        self._last_hash[rel] = new_hash
        self._remember_apply(rel, new_hash)
        self._remote_applied += 1
        _logger.info(
            "cloud-files: applied remote MODIFY %s (%d bytes, version=%s)",
            rel,
            len(data),
            change.current_version,
        )

    # ─── Status / Stats (A3 + A4) ────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Snapshot for /internal/cloud-sync-status. Designed to be cheap."""
        m = self._metrics
        queue_stats = (
            self._persistent_queue.stats()
            if self._persistent_queue
            else {"enqueued": 0, "done": 0, "pending": 0, "bytes": 0}
        )
        inflight = 0
        if self._inflight_sem is not None:
            # Best-effort — semaphores don't expose count directly; approximate
            # via the pending dict (paths with timers + paths actively flushing).
            inflight = len(self._pending)
        sub_kind = type(self._subscriber).__name__ if self._subscriber else None
        return {
            "mode": self._mode,
            "mode_since_ts": self._mode_since,
            "started_at_ts": self._started_at,
            "is_running": self._mode in ("active", "degraded"),
            "cloud_root": str(self.cloud_root),
            "ai_dream_configured": self._cfg is not None,
            "ai_dream_url": self._cfg.url if self._cfg else None,
            "user_id": self._cfg.user_id if self._cfg else None,
            "pending": len(self._pending),
            "inflight_approx": inflight,
            # The working set filled up. Every counted event is STILL PENDING in
            # the durable queue — the cap stops admission, never the work.
            "backpressure_events": {
                "total": self._backpressure_events,
                "deferred_now": len(self._deferred),
                "awaiting_restart": self._backpressure_unindexed,
                "working_set_cap": MAX_PENDING,
            },
            "seeded_hashes": len(self._last_hash),
            "metrics": {
                "puts": m.put_count,
                "deletes": m.delete_count,
                "bytes_uploaded": m.bytes_uploaded,
                "errors_total": m.errors_total,
                "last_success_ts": m.last_success_ts,
                "last_error_ts": m.last_error_ts,
                "last_error_message": m.last_error_message,
                "latency_ms_p50": m.percentile(0.50),
                "latency_ms_p95": m.percentile(0.95),
                "latency_ms_p99": m.percentile(0.99),
                "recent_errors": list(m.recent_errors)[:8],
            },
            "queue": queue_stats,
            # Writes AI Dream refused. A non-zero count means a person has
            # edits that are NOT in the cloud yet and the reason is right here
            # — never a silent loss (this endpoint is what the frontend's sync
            # indicator and the orchestrator read).
            "held_writes": self._held_status(),
            "downstream": {
                "subscriber": sub_kind,
                "received": self._remote_received,
                "applied": self._remote_applied,
                "echo_suppressed": self._remote_echo_suppressed,
                "recently_applied_size": len(self._recently_applied),
                # A 4xx on the DOWN direction (the box is refused the change
                # feed) means this sandbox stopped receiving the user's cloud
                # edits. Same rule as a held write: it is named, not logged and
                # forgotten.
                **(
                    self._subscriber.status()
                    if hasattr(self._subscriber, "status")
                    else {}
                ),
            },
        }

    def get_stats(self) -> dict[str, Any]:
        """Compact snapshot for embedding in the session manifest."""
        m = self._metrics
        return {
            "mode": self._mode,
            "puts": m.put_count,
            "deletes": m.delete_count,
            "bytes_uploaded": m.bytes_uploaded,
            "errors_total": m.errors_total,
            "last_success_ts": m.last_success_ts,
            "last_error_message": m.last_error_message,
            "latency_ms_p50": m.percentile(0.50),
            "latency_ms_p95": m.percentile(0.95),
            "queue_pending": (
                self._persistent_queue.stats()["pending"]
                if self._persistent_queue
                else 0
            ),
            "held_writes": len(self._held),
            "backpressure_events": self._backpressure_events,
            "backpressure_deferred": len(self._deferred),
            "held_last_refusal": (
                refusal_sentence(
                    max(
                        self._held.values(), key=lambda h: h.last_attempt_ts
                    ).refusal
                )
                if self._held
                else None
            ),
            "downstream_received": self._remote_received,
            "downstream_applied": self._remote_applied,
            "downstream_echo_suppressed": self._remote_echo_suppressed,
            "downstream_subscriber": (
                type(self._subscriber).__name__ if self._subscriber else None
            ),
        }
