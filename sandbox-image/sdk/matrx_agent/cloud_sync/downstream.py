"""Downstream subscribers — the cloud → sandbox half of the sync.

Two implementations:

- ``PollingSubscriber`` — calls the bridge's ``/api/cloud-files/changes?since=…``
  endpoint every 30s, finds rows with ``updated_at > since``, and dispatches
  one ``RemoteChange`` per row to the watcher. Always available; works on
  ``:core`` and ``:aidream`` images alike. Doesn't surface deletions in v1
  (the bridge doesn't expose them — see ``cloud_files_bridge.py::list_changes``).

- ``RealtimeSubscriber`` — uses Supabase Realtime over a Postgres-WAL-backed
  WebSocket to receive INSERT/UPDATE/DELETE events on ``cld_files`` filtered
  by ``owner_id=eq.<USER_ID>``. Sub-second latency. Requires the operator to
  have applied ``aidream/db/migrations/0002_cld_files_realtime.sql`` and the
  sandbox image to ship the ``realtime`` Python package + Supabase URL/key
  env passthrough. Falls back to ``PollingSubscriber`` on any connection
  failure.

The watcher uses ``make_subscriber()`` which returns the best implementation
available for the current sandbox.

Both implementations call back into a single async callback:

    async def on_change(change: RemoteChange) -> None: ...

The watcher's callback writes the bytes to disk, updates ``_last_hash`` so
the watchdog event the local FS write triggers gets de-duped by the existing
hash short-circuit in ``_flush_upsert``, and additionally pushes the rel-path
into a recently-applied LRU as belt-and-braces against the echo loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

from matrx_agent.cloud_sync.client import AsyncBridgeClient, BridgeConfig

_logger = logging.getLogger("matrx_agent.cloud_sync.downstream")

POLL_INTERVAL_SECONDS = 30.0
POLL_BACKOFF_INITIAL = 5.0
POLL_BACKOFF_MAX = 300.0  # 5 min
REALTIME_RETRY_INTERVAL = 300.0  # try Realtime again every 5 min when we've fallen back to polling


@dataclass(frozen=True)
class RemoteChange:
    """Normalised change event from any subscriber."""
    kind: str  # "modified" | "deleted"
    rel_path: str
    file_size: Optional[int] = None
    checksum: Optional[str] = None
    current_version: Optional[int] = None
    updated_at: Optional[str] = None


OnChange = Callable[[RemoteChange], Awaitable[None]]


# ──────────────────────────────────────────────────────────────────────────
# Polling
# ──────────────────────────────────────────────────────────────────────────


class PollingSubscriber:
    """Hit /api/cloud-files/changes on a fixed interval; dispatch new rows.

    Cursor management: starts at "now", remembers the latest ``updated_at``
    seen across all polled rows, hands that back as ``since`` next round.
    Robust to clock skew between sandbox and AI Dream — the cursor is
    AI-Dream-relative since the bridge echoes whatever timestamps it has.
    """

    def __init__(self, client: AsyncBridgeClient):
        self._client = client
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._cursor_iso = _now_iso()

    async def start(self, on_change: OnChange) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(on_change))
        _logger.info("cloud-files: PollingSubscriber started (interval=%.0fs)", POLL_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self, on_change: OnChange) -> None:
        backoff = POLL_BACKOFF_INITIAL
        while not self._stop.is_set():
            try:
                # ``list_changes`` was monkey-patched onto AsyncBridgeClient by
                # client.py — gracefully no-op if a stale image is missing it.
                fn = getattr(self._client, "list_changes", None)
                if fn is None:
                    _logger.warning("cloud-files: AsyncBridgeClient.list_changes missing — disabling polling")
                    return
                envelope: dict[str, Any] = await fn(self._cursor_iso)
                rows = envelope.get("files") or []
                next_cursor = envelope.get("next_cursor") or self._cursor_iso

                for rec in rows:
                    rel = rec.get("file_path")
                    if not rel:
                        continue
                    change = RemoteChange(
                        kind="modified",
                        rel_path=rel,
                        file_size=rec.get("file_size"),
                        checksum=rec.get("checksum"),
                        current_version=rec.get("current_version"),
                        updated_at=rec.get("updated_at"),
                    )
                    try:
                        await on_change(change)
                    except Exception as e:  # noqa: BLE001
                        _logger.warning("cloud-files: on_change handler raised for %s: %s", rel, e)

                self._cursor_iso = next_cursor
                backoff = POLL_BACKOFF_INITIAL
            except asyncio.CancelledError:
                return
            except (httpx.HTTPError, Exception) as e:  # noqa: BLE001
                _logger.warning("cloud-files: polling cycle failed: %s (retrying in %.0fs)", e, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, POLL_BACKOFF_MAX)
                continue

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
                return  # stop signal
            except asyncio.TimeoutError:
                pass


# ──────────────────────────────────────────────────────────────────────────
# Realtime (optional)
# ──────────────────────────────────────────────────────────────────────────


def _realtime_available() -> bool:
    """True iff the optional ``realtime`` package is importable AND the
    Supabase URL/key are present in env. Used by ``make_subscriber`` to
    decide whether to attempt Realtime at all.
    """
    if not _supabase_creds():
        return False
    try:
        import realtime  # noqa: F401
        return True
    except ImportError:
        return False


def _supabase_creds() -> Optional[tuple[str, str]]:
    url = (
        os.environ.get("SUPABASE_URL")
        or os.environ.get("SUPABASE_MATRIX_URL")
        or ""
    )
    key = (
        os.environ.get("SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_SECRET_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_MATRIX_KEY")
        or ""
    )
    if url and key:
        return (url, key)
    return None


class RealtimeSubscriber:
    """Listen to Postgres WAL events on ``cld_files`` filtered by owner_id.

    Runs Realtime if the env + lib are present; transparently falls back to
    the inner ``PollingSubscriber`` on any failure. Re-attempts Realtime
    every REALTIME_RETRY_INTERVAL seconds even after a fallback so a flaky
    network doesn't condemn us to polling for the rest of the session.
    """

    def __init__(self, cfg: BridgeConfig, fallback: PollingSubscriber):
        self._cfg = cfg
        self._fallback = fallback
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._connected = False

    async def start(self, on_change: OnChange) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._supervisor(on_change))
        _logger.info("cloud-files: RealtimeSubscriber started")

    async def stop(self) -> None:
        self._stop.set()
        await self._fallback.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _supervisor(self, on_change: OnChange) -> None:
        """Try Realtime; on any error, hand the channel over to the polling
        fallback and retry Realtime in the background.
        """
        polling_active = False
        while not self._stop.is_set():
            ok = await self._try_realtime(on_change)
            if ok:
                # Realtime ran and exited cleanly (e.g. shutdown signal).
                if polling_active:
                    await self._fallback.stop()
                return
            # Realtime failed. Run polling in the meantime.
            if not polling_active:
                _logger.warning("cloud-files: Realtime unavailable, switching to polling fallback")
                await self._fallback.start(on_change)
                polling_active = True
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=REALTIME_RETRY_INTERVAL)
                return
            except asyncio.TimeoutError:
                _logger.info("cloud-files: retrying Realtime after %.0fs of polling", REALTIME_RETRY_INTERVAL)

    async def _try_realtime(self, on_change: OnChange) -> bool:
        """Attempt one Realtime session. Returns True on clean exit (stop
        requested), False on any error or unsupported environment.
        """
        creds = _supabase_creds()
        if creds is None:
            return False
        url, key = creds

        try:
            from realtime import AsyncRealtimeClient  # type: ignore
        except ImportError:
            return False

        ws_url = url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        client = AsyncRealtimeClient(f"{ws_url}/realtime/v1/websocket", key)
        try:
            await client.connect()
        except Exception as e:  # noqa: BLE001
            _logger.warning("cloud-files: realtime connect failed: %s", e)
            return False

        self._connected = True
        try:
            channel = client.channel(f"realtime:public:cld_files:owner_id=eq.{self._cfg.user_id}")

            async def _on_postgres_change(payload: dict) -> None:
                try:
                    await self._dispatch(payload, on_change)
                except Exception as e:  # noqa: BLE001
                    _logger.warning("cloud-files: realtime dispatch failed: %s", e)

            channel.on_postgres_changes(
                event="*",
                schema="public",
                table="cld_files",
                callback=_on_postgres_change,
                filter=f"owner_id=eq.{self._cfg.user_id}",
            )
            await channel.subscribe()
            _logger.info("cloud-files: realtime subscribed to cld_files for user")

            # Block until shutdown is requested.
            await self._stop.wait()
            try:
                await channel.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("cloud-files: realtime channel error: %s", e)
            return False
        finally:
            self._connected = False
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch(self, payload: dict, on_change: OnChange) -> None:
        """Translate a Supabase Realtime payload into RemoteChange events.

        Payload shape (Supabase Realtime v2):
            {
                "schema": "public",
                "table": "cld_files",
                "commit_timestamp": "...",
                "eventType": "INSERT" | "UPDATE" | "DELETE",
                "new": {...} | None,
                "old": {...} | None,
            }
        """
        event = (payload.get("eventType") or payload.get("type") or "").upper()
        new = payload.get("new") or {}
        old = payload.get("old") or {}

        if event == "DELETE":
            rel = old.get("file_path") or new.get("file_path")
            if not rel:
                return
            await on_change(RemoteChange(kind="deleted", rel_path=rel))
            return

        # INSERT / UPDATE
        rel = new.get("file_path")
        if not rel:
            return
        # Soft-delete shows up as UPDATE with deleted_at set.
        if new.get("deleted_at"):
            await on_change(RemoteChange(kind="deleted", rel_path=rel))
            return
        await on_change(RemoteChange(
            kind="modified",
            rel_path=rel,
            file_size=new.get("file_size"),
            checksum=new.get("checksum"),
            current_version=new.get("current_version"),
            updated_at=new.get("updated_at"),
        ))


# ──────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────


def make_subscriber(
    client: AsyncBridgeClient,
    cfg: BridgeConfig,
):
    """Pick the best subscriber for the current sandbox.

    - If Realtime is configured (creds + lib), return a RealtimeSubscriber
      that wraps a PollingSubscriber as its fallback.
    - Otherwise, return a bare PollingSubscriber.
    """
    polling = PollingSubscriber(client)
    if _realtime_available():
        return RealtimeSubscriber(cfg, polling)
    return polling


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
