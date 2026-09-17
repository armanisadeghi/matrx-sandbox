"""The downstream half of the sync: cloud → sandbox. ONE transport, announced.

``PollingSubscriber`` calls the bridge's ``/api/cloud-files/changes?since=…``,
dispatches one ``RemoteChange`` per row to the watcher, and follows the poll
cadence the server asks for. It is the only downstream transport there is.

A ``RealtimeSubscriber`` lived here until 2026-09-17. It opened a WebSocket
straight to the PLATFORM DATABASE (Supabase Realtime on ``cld_files``), scoped
by nothing but a client-side ``owner_id`` filter — no organization, no bridge,
no admission control — using whichever of five Supabase key names happened to
be in the container env. It was dead code by construction: the orchestrator
never injects any of those names (``*_SECRET*`` / ``*_KEY*`` are on the
platform-env deny-list, docs/incidents/2026-09-13-platform-env-leak.md), so
``_realtime_available()`` was always false and ``make_subscriber`` fell back to
polling WITHOUT SAYING SO. It is deleted, not disabled, for two reasons:

  * **A sandbox never talks to the platform database directly.** The bridge is
    the one hop, and the bridge is what carries the organization — the law is
    THE REQUEST CONTEXT IS CARRIED, NEVER REBUILT
    (``common-docs/policies/context-is-carried-never-rebuilt.md``). A WAL
    subscription filtered client-side is the opposite of that.
  * Reviving it would have needed a platform database key inside every user's
    box, which is exactly the leak the platform spent an incident closing.

**Deletions.** With Realtime gone, deletions must arrive on the one hop, and
today they do not: ``/api/cloud-files/changes`` returns modifications only and
says so with ``deletions_supported: false``. A file the user deletes in the UI
therefore stays on disk in the sandbox, and the shutdown up-sync RESURRECTS it.
That is a real, user-visible defect, so this subscriber (a) honours the
deletion markers the moment the bridge ships them and (b) says the consequence
out loud, once, when the bridge reports it cannot send them — never a silent
best-effort. The exact contract the two halves meet on is written down in
``sandbox-image/sdk/matrx_agent/cloud_sync/FEATURE.md`` § The ``/changes``
contract; the aidream side of it is another lane's.

Both call back into a single async callback:

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
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

from matrx_agent.cloud_sync.client import AsyncBridgeClient, BridgeConfig

_logger = logging.getLogger("matrx_agent.cloud_sync.downstream")

#: The cadence used until the server says otherwise. The bridge sends its own
#: instruction with EVERY answer (``poll_after_seconds``, mirrored in the
#: ``Retry-After`` header), and the loop follows it — that is how 226 boxes
#: back off together the moment the feed starts shedding, instead of each one
#: discovering the congestion by being refused (2026-09-14: 61 polls shed
#: across 49 users in one minute while every box held a fixed 30 s timer).
#: Server-side knobs: ``infrastructure.sandbox`` /
#: ``change_feed_poll_interval_seconds`` and ``…_under_pressure_seconds``.
POLL_INTERVAL_SECONDS = 30.0
#: A server instruction is honoured only inside this band: below the floor a
#: bad value would turn one box into a hammer, above the ceiling it would
#: silently stop syncing. Outside the band the loop keeps its own cadence and
#: says so — never a silent clamp to something nobody asked for.
POLL_INTERVAL_MIN_SECONDS = 5.0
POLL_INTERVAL_MAX_SECONDS = 600.0
POLL_BACKOFF_INITIAL = 5.0
POLL_BACKOFF_MAX = 300.0  # 5 min
# Every wait is jittered by this fraction so a fleet of sandboxes that started
# together does not poll AI Dream in the same second forever (2026-09-12: 43
# pollers landed inside one second and consumed the server's pool; the server
# now sheds such herds with a 503 + Retry-After, which the loop honours).
POLL_JITTER_FRACTION = 0.2


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
        #: The cadence the server last asked for, or the built-in one until it
        #: does. Kept on the subscriber so a single instruction survives the
        #: next cycle rather than being re-learned each round.
        self._interval_seconds = POLL_INTERVAL_SECONDS
        #: Whether the bridge has told us it can send deletions, and whether we
        #: have already said what that means. Announced ONCE per session, on
        #: the first answer — never per poll, never not at all.
        self._deletions_supported: Optional[bool] = None
        self._announced_deletion_support = False

    async def start(self, on_change: OnChange) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(on_change))
        _logger.info(
            "cloud-files: downstream sync is polling the bridge change feed "
            "every %.0fs (deletion support is reported by the server on the "
            "first answer)", POLL_INTERVAL_SECONDS,
        )

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

                self._note_deletion_support(envelope.get("deletions_supported"))

                for rec in rows:
                    rel = rec.get("file_path")
                    if not rel:
                        continue
                    if _row_is_deleted(rec):
                        change = RemoteChange(kind="deleted", rel_path=rel)
                    else:
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
                self._adopt_server_interval(envelope.get("poll_after_seconds"))
                backoff = POLL_BACKOFF_INITIAL
            except asyncio.CancelledError:
                return
            except (httpx.HTTPError, Exception) as e:  # noqa: BLE001
                retry_after = _retry_after_seconds(e)
                # Jittered in BOTH branches: the server randomises Retry-After per
                # response today, but this loop must not depend on that staying true.
                wait = _jittered(retry_after if retry_after is not None else backoff)
                _logger.warning(
                    "cloud-files: polling cycle failed: %s (retrying in %.0fs%s)",
                    e,
                    wait,
                    " as the server's Retry-After asked" if retry_after is not None else "",
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, POLL_BACKOFF_MAX)
                continue

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=_jittered(self._interval_seconds)
                )
                return  # stop signal
            except asyncio.TimeoutError:
                pass

    def _note_deletion_support(self, raw: Any) -> None:
        """Say — once — whether deletions reach this sandbox at all.

        ``deletions_supported: false`` is not a detail: a file the user deleted
        in AI Dream stays on disk here, and the shutdown up-sync pushes it back,
        so the delete appears to undo itself. Nothing may fail silently, so the
        box states the consequence and the remedy instead of quietly syncing
        half the truth. The contract is in this package's FEATURE.md.
        """
        supported = bool(raw)
        self._deletions_supported = supported
        if self._announced_deletion_support:
            return
        self._announced_deletion_support = True
        if supported:
            _logger.info(
                "cloud-files: the bridge change feed carries deletions — a file "
                "deleted in AI Dream is removed from this sandbox too.",
            )
        else:
            _logger.warning(
                "cloud-files: the bridge change feed reports "
                "deletions_supported=false, so a file DELETED in AI Dream is "
                "NOT removed from this sandbox and the shutdown up-sync will "
                "restore it in the cloud. Remedy: AI Dream's "
                "/api/cloud-files/changes must include soft-deleted rows marked "
                "deleted=true and set deletions_supported=true (contract: "
                "matrx_agent/cloud_sync/FEATURE.md § The /changes contract). "
                "Until it does, deletions only converge on the next session's "
                "bulk down-sync.",
            )

    def _adopt_server_interval(self, raw: Any) -> None:
        """Take the bridge's ``poll_after_seconds`` instruction, or say why not.

        An answer the server did not annotate (an older bridge) leaves the
        cadence alone, which is the pre-2026-09-14 behaviour.
        """
        if raw is None:
            return
        try:
            asked = float(raw)
        except (TypeError, ValueError):
            _logger.warning(
                "cloud-files: the bridge asked for a poll interval of %r, which is "
                "not a number — keeping %.0fs.", raw, self._interval_seconds,
            )
            return
        if not (POLL_INTERVAL_MIN_SECONDS <= asked <= POLL_INTERVAL_MAX_SECONDS):
            _logger.warning(
                "cloud-files: the bridge asked for a poll interval of %.0fs, outside "
                "the %.0f-%.0fs this image accepts — keeping %.0fs.",
                asked, POLL_INTERVAL_MIN_SECONDS, POLL_INTERVAL_MAX_SECONDS,
                self._interval_seconds,
            )
            return
        if asked != self._interval_seconds:
            _logger.info(
                "cloud-files: the bridge asked for a %.0fs poll interval (was %.0fs) — "
                "following it.", asked, self._interval_seconds,
            )
            self._interval_seconds = asked


def _row_is_deleted(rec: dict) -> bool:
    """True when the bridge marked this change-feed row as a deletion.

    ``deleted: true`` is the contract (FEATURE.md § The /changes contract);
    ``deleted_at`` is accepted as the same statement because it is the column
    the soft-delete actually writes, and a row carrying it can never be a
    modification.
    """
    if rec.get("deleted") is True:
        return True
    return bool(rec.get("deleted_at"))


def _jittered(seconds: float) -> float:
    """``seconds`` ± ``POLL_JITTER_FRACTION``, never below one second."""
    spread = seconds * POLL_JITTER_FRACTION
    return max(1.0, seconds + random.uniform(-spread, spread))


def _retry_after_seconds(error: BaseException) -> Optional[float]:
    """The server's ``Retry-After`` (seconds) on a 429/503, clamped to the backoff
    ceiling; ``None`` when the failure carried no such instruction.

    The bridge sheds a poll with an honest 503 + Retry-After when the server's
    pool is under pressure; retrying sooner than asked is what turned one stall
    into a herd. A non-numeric or missing header falls back to the local backoff.
    """
    if not isinstance(error, httpx.HTTPStatusError):
        return None
    if error.response.status_code not in {429, 503}:
        return None
    raw = error.response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(seconds, POLL_BACKOFF_MAX)


# ──────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────


def make_subscriber(client: AsyncBridgeClient, cfg: BridgeConfig):
    """The downstream subscriber for this sandbox.

    There is exactly one, and it says which one it is: a sandbox that quietly
    changed transport is a sandbox nobody can reason about. ``cfg`` is accepted
    (and unused) so the watcher's call site is unchanged now that the direct
    database subscriber it used to select is gone.
    """
    _logger.info(
        "cloud-files: downstream transport is the AI Dream bridge change feed "
        "(polling /api/cloud-files/changes). It is the only one — a sandbox "
        "never subscribes to the platform database directly.",
    )
    return PollingSubscriber(client)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
