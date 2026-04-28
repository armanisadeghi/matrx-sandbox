"""Real-time watcher for ~/cloud-files/ → AI Dream cld_files.

Observes the cloud-files directory via watchdog, debounces events per-path
for 5 seconds, then pushes through the cloud-files bridge. Stays dormant if
AI Dream env vars are missing or if the down-sync marker hasn't been written
yet (so we don't re-upload everything the down-sync just pulled).

Lifecycle is owned by the matrx_agent FastAPI daemon's lifespan.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from matrx_agent.cloud_sync.client import AsyncBridgeClient, BridgeConfig

_logger = logging.getLogger("matrx_agent.cloud_sync")

DEBOUNCE_SECONDS = 5.0
MAX_FILE_SIZE = 1 << 30  # 1 GiB — matches bridge per-request cap
MAX_PENDING = 10_000
MAX_INFLIGHT = 16
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
SEED_HASH_CHUNK = 1 << 20  # 1 MiB
STABILITY_WAIT_SECONDS = 0.2
DOWN_MARKER_WAIT_SECONDS = 60.0
DOWN_MARKER_POLL_INTERVAL = 0.5


class _Handler(FileSystemEventHandler):
    """Watchdog handler — runs in the observer thread, hands events to the asyncio loop."""

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.queue = queue
        self.loop = loop

    def _enqueue(self, kind: str, path: str) -> None:
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


class CloudFilesWatcher:
    """In-process watcher that pushes ~/cloud-files/ changes to AI Dream in real time."""

    def __init__(
        self,
        cloud_root: Path = Path("/home/agent/cloud-files"),
        marker_path: Path = Path("/home/agent/.matrx/runtime/cloud-files-down-complete"),
    ):
        self.cloud_root = cloud_root
        self.marker_path = marker_path
        self._cfg: Optional[BridgeConfig] = None
        self._client: Optional[AsyncBridgeClient] = None
        self._observer: Optional[Observer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._pending: "OrderedDict[str, asyncio.TimerHandle]" = OrderedDict()
        self._inflight_sem: Optional[asyncio.Semaphore] = None
        self._last_hash: dict[str, str] = {}
        self._event_arrivals: dict[str, float] = {}
        self._running = False

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._cfg = BridgeConfig.from_env()
        if self._cfg is None:
            _logger.info("cloud-files: AI Dream env not configured — watcher dormant")
            return

        if not await self._await_down_marker():
            _logger.warning(
                "cloud-files: down-sync marker %s not seen in %.0fs — watcher dormant",
                self.marker_path, DOWN_MARKER_WAIT_SECONDS,
            )
            return

        if not self.cloud_root.exists():
            _logger.warning(
                "cloud-files: %s does not exist — watcher dormant", self.cloud_root,
            )
            return

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._inflight_sem = asyncio.Semaphore(MAX_INFLIGHT)
        self._client = AsyncBridgeClient(self._cfg)

        self._seed_hashes()

        self._observer = Observer()
        self._observer.schedule(
            _Handler(self._queue, self._loop), str(self.cloud_root), recursive=True,
        )
        self._observer.start()

        self._running = True
        self._drain_task = asyncio.create_task(self._drain())
        _logger.info(
            "cloud-files: watcher started (root=%s, %d seeded hashes, debounce=%.1fs)",
            self.cloud_root, len(self._last_hash), DEBOUNCE_SECONDS,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for handle in list(self._pending.values()):
            handle.cancel()
        self._pending.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
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
        _logger.info("cloud-files: watcher stopped")

    # ─── Setup helpers ───────────────────────────────────────────────────────

    async def _await_down_marker(self) -> bool:
        """Block (with cap) until the bulk down-sync writes its completion marker."""
        deadline = asyncio.get_running_loop().time() + DOWN_MARKER_WAIT_SECONDS
        while True:
            if self.marker_path.exists():
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(DOWN_MARKER_POLL_INTERVAL)

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
                if p.stat().st_size > MAX_FILE_SIZE:
                    continue
                self._last_hash[rel] = self._sha256(p)
            except OSError:
                continue

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
        assert self._queue is not None and self._loop is not None
        while self._running:
            try:
                kind, abs_path, t_arrival = await self._queue.get()
            except asyncio.CancelledError:
                return

            try:
                p = Path(abs_path)
                rel = self._rel_path(p)
                if rel is None:
                    _logger.debug("cloud-files: ignoring %s (outside root)", abs_path)
                    continue
                if self._is_dotpath(rel):
                    continue

                old = self._pending.pop(rel, None)
                if old is not None:
                    old.cancel()

                if len(self._pending) >= MAX_PENDING:
                    drop_rel, drop_handle = self._pending.popitem(last=False)
                    drop_handle.cancel()
                    _logger.warning(
                        "cloud-files: backpressure cap reached, dropping pending %s",
                        drop_rel,
                    )

                self._event_arrivals.setdefault(rel, t_arrival)
                if kind == "delete":
                    handle = self._loop.call_later(
                        DEBOUNCE_SECONDS,
                        lambda r=rel: asyncio.create_task(self._flush_delete(r)),
                    )
                else:
                    handle = self._loop.call_later(
                        DEBOUNCE_SECONDS,
                        lambda r=rel: asyncio.create_task(self._flush_upsert(r)),
                    )
                self._pending[rel] = handle
            except Exception as e:  # noqa: BLE001
                _logger.exception("cloud-files: drain error: %s", e)

    # ─── Flush handlers ──────────────────────────────────────────────────────

    async def _flush_upsert(self, rel: str) -> None:
        self._pending.pop(rel, None)
        assert self._inflight_sem is not None and self._client is not None
        async with self._inflight_sem:
            local = self.cloud_root / rel
            try:
                if local.is_symlink():
                    _logger.info(
                        "cloud-files: skipping %s (symlink, v1 limitation)", rel,
                    )
                    self._event_arrivals.pop(rel, None)
                    return
                if not local.exists():
                    # Created-then-deleted within the debounce window. Not an error.
                    self._event_arrivals.pop(rel, None)
                    return
                size = local.stat().st_size
                if size > MAX_FILE_SIZE:
                    _logger.info(
                        "cloud-files: skipping %s (>1 GiB cap, size=%d)", rel, size,
                    )
                    self._event_arrivals.pop(rel, None)
                    return

                # Stability check: re-stat after a short wait. If size is still moving,
                # the agent is mid-write — re-queue with a fresh debounce.
                await asyncio.sleep(STABILITY_WAIT_SECONDS)
                if not local.exists():
                    self._event_arrivals.pop(rel, None)
                    return
                if local.stat().st_size != size:
                    if self._loop is not None:
                        handle = self._loop.call_later(
                            DEBOUNCE_SECONDS,
                            lambda r=rel: asyncio.create_task(self._flush_upsert(r)),
                        )
                        self._pending[rel] = handle
                    return

                new_hash = self._sha256(local)
                if self._last_hash.get(rel) == new_hash:
                    self._event_arrivals.pop(rel, None)
                    return

                t0 = self._event_arrivals.get(rel, time.monotonic())
                last_err: Optional[Exception] = None
                for delay in (0.0, *RETRY_DELAYS):
                    if delay > 0:
                        await asyncio.sleep(delay)
                    try:
                        result = await self._client.put_one(local, rel)
                        latency_ms = int((time.monotonic() - t0) * 1000)
                        is_new = (
                            isinstance(result, dict)
                            and result.get("version") == 1
                        )
                        _logger.info(
                            "cloud-files: PUT %s %d bytes new=%s latency_ms=%d",
                            rel, size, is_new, latency_ms,
                        )
                        self._last_hash[rel] = new_hash
                        self._event_arrivals.pop(rel, None)
                        return
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                _logger.warning(
                    "cloud-files: PUT %s failed after retries: %s", rel, last_err,
                )
                self._event_arrivals.pop(rel, None)
            except Exception as e:  # noqa: BLE001
                _logger.exception("cloud-files: flush_upsert error for %s: %s", rel, e)
                self._event_arrivals.pop(rel, None)

    async def _flush_delete(self, rel: str) -> None:
        self._pending.pop(rel, None)
        assert self._inflight_sem is not None and self._client is not None
        async with self._inflight_sem:
            local = self.cloud_root / rel
            try:
                # If the path came back to life inside the debounce window, treat
                # it as an upsert instead of a delete.
                if local.exists() and not local.is_symlink():
                    if self._loop is not None:
                        handle = self._loop.call_later(
                            0.0,
                            lambda r=rel: asyncio.create_task(self._flush_upsert(r)),
                        )
                        self._pending[rel] = handle
                    return

                t0 = self._event_arrivals.get(rel, time.monotonic())
                last_err: Optional[Exception] = None
                for delay in (0.0, *RETRY_DELAYS):
                    if delay > 0:
                        await asyncio.sleep(delay)
                    try:
                        await self._client.delete_one(rel)
                        latency_ms = int((time.monotonic() - t0) * 1000)
                        _logger.info(
                            "cloud-files: DELETE %s latency_ms=%d", rel, latency_ms,
                        )
                        self._last_hash.pop(rel, None)
                        self._event_arrivals.pop(rel, None)
                        return
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                _logger.warning(
                    "cloud-files: DELETE %s failed after retries: %s", rel, last_err,
                )
                self._event_arrivals.pop(rel, None)
            except Exception as e:  # noqa: BLE001
                _logger.exception("cloud-files: flush_delete error for %s: %s", rel, e)
                self._event_arrivals.pop(rel, None)
