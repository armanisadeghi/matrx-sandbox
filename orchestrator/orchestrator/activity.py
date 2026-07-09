"""Per-box activity tracking + migration lock (zero-drift system, Phase 2a).

The orchestrator proxies every agent tool call (exec / fs / exec-stream) to the
in-container daemon, so it knows EXACTLY when a box is mid-operation vs. idle —
no cross-service polling, no guessing. We use that to make migration safe and
invisible to the agent:

  - While a box is being migrated it is *locked*: any tool call that lands during
    the swap window gets a retryable 503 ("migrating"), and the agent's tool
    proxy retries onto the new container (same sandbox_id, same binding). No data
    loss, no confusion — just a few seconds of transparent retry.
  - Before cutting over, the migrator DRAINS: it waits for in-flight calls to
    finish so it never kills a tool mid-execution. If they don't drain within a
    window, the box is left alone and retried later (busy boxes migrate at their
    next idle gap, never by force).

Process-local state. Each orchestrator guards its own host's boxes, which is
exactly the scope that matters (a box lives on one host).
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class SandboxMigratingError(Exception):
    """Raised when a tool call hits a box that is mid-migration. Maps to 503."""


_inflight: dict[str, int] = {}
_migrating: set[str] = set()
_cond = asyncio.Condition()

# Idle-gate signals beyond in-flight counts (an agent "between commands" and a
# human with an open terminal both look idle to _inflight alone):
#   _last_activity — monotonic timestamp of the most recent tool-call FINISH.
#   _open_sessions — live interactive attachments (PTY terminals, fs-watch
#     websockets). ANY open session = busy: never swap a box out from under an
#     attached human/editor. Process-local; an orchestrator restart clears them
#     (the in-flight + heartbeat gates still apply after).
_last_activity: dict[str, float] = {}
_open_sessions: dict[str, int] = {}


def note_activity(sandbox_id: str) -> None:
    _last_activity[sandbox_id] = time.monotonic()


def last_activity_age(sandbox_id: str) -> float | None:
    """Seconds since the last tracked tool call finished, or None if never."""
    ts = _last_activity.get(sandbox_id)
    return None if ts is None else time.monotonic() - ts


def session_opened(sandbox_id: str) -> None:
    _open_sessions[sandbox_id] = _open_sessions.get(sandbox_id, 0) + 1
    note_activity(sandbox_id)


def session_closed(sandbox_id: str) -> None:
    n = _open_sessions.get(sandbox_id, 0) - 1
    if n <= 0:
        _open_sessions.pop(sandbox_id, None)
    else:
        _open_sessions[sandbox_id] = n
    note_activity(sandbox_id)


def open_session_count(sandbox_id: str) -> int:
    return _open_sessions.get(sandbox_id, 0)


def is_migrating(sandbox_id: str) -> bool:
    return sandbox_id in _migrating


def inflight_count(sandbox_id: str) -> int:
    """How many tool calls are executing against this box right now. The rolling
    migrator uses this to migrate ONLY genuinely-idle boxes (0 in-flight), so a
    call already mid-execution is never caught by a migration it can't 503-retry."""
    return _inflight.get(sandbox_id, 0)


@asynccontextmanager
async def track(sandbox_id: str):
    """Count one in-flight tool call against a box. Refuses (raises
    SandboxMigratingError) if the box is mid-migration so the caller can return a
    retryable 503 instead of racing the swap."""
    async with _cond:
        if sandbox_id in _migrating:
            raise SandboxMigratingError(sandbox_id)
        _inflight[sandbox_id] = _inflight.get(sandbox_id, 0) + 1
    try:
        yield
    finally:
        note_activity(sandbox_id)
        async with _cond:
            n = _inflight.get(sandbox_id, 0) - 1
            if n <= 0:
                _inflight.pop(sandbox_id, None)
            else:
                _inflight[sandbox_id] = n
            _cond.notify_all()


async def mark_migrating(sandbox_id: str) -> None:
    """Lock the box for the WHOLE migration (call at migrate start). From here on
    every new tool call is refused with a retryable 503 (see is_migrating + the
    route guards), so calls don't hit the brief windows where the box's row/
    container is in flux — they just retry and land on the migrated box. Must be
    paired with release_migration() in a finally."""
    async with _cond:
        _migrating.add(sandbox_id)


async def drain_inflight(sandbox_id: str, *, timeout: float = 20.0) -> bool:
    """Wait for calls that were already in-flight when we marked migrating to
    finish, so cutover never interrupts a tool mid-execution. Returns False if
    they don't drain in time (caller defers the migration). New calls are already
    refused via mark_migrating, so in-flight only shrinks."""
    async with _cond:
        try:
            await asyncio.wait_for(
                _cond.wait_for(lambda: _inflight.get(sandbox_id, 0) == 0),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "migration drain timed out for %s (in_flight=%d) — deferring",
                sandbox_id, _inflight.get(sandbox_id, 0),
            )
            return False


async def release_migration(sandbox_id: str) -> None:
    async with _cond:
        _migrating.discard(sandbox_id)
        _cond.notify_all()
