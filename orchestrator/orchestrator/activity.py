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
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class SandboxMigratingError(Exception):
    """Raised when a tool call hits a box that is mid-migration. Maps to 503."""


_inflight: dict[str, int] = {}
_migrating: set[str] = set()
_cond = asyncio.Condition()


def is_migrating(sandbox_id: str) -> bool:
    return sandbox_id in _migrating


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
        async with _cond:
            n = _inflight.get(sandbox_id, 0) - 1
            if n <= 0:
                _inflight.pop(sandbox_id, None)
            else:
                _inflight[sandbox_id] = n
            _cond.notify_all()


async def acquire_migration(sandbox_id: str, *, drain_timeout: float = 20.0) -> bool:
    """Lock the box for migration and wait for in-flight calls to drain.

    Returns True once locked AND idle (safe to swap). Returns False if calls did
    not drain within ``drain_timeout`` — the lock is released and the caller must
    NOT migrate now (defer + retry later). New calls are refused (503) for the
    whole time the lock is held."""
    async with _cond:
        _migrating.add(sandbox_id)
    try:
        async with _cond:
            try:
                await asyncio.wait_for(
                    _cond.wait_for(lambda: _inflight.get(sandbox_id, 0) == 0),
                    timeout=drain_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "migration drain timed out for %s (in_flight=%d) — deferring",
                    sandbox_id, _inflight.get(sandbox_id, 0),
                )
                _migrating.discard(sandbox_id)
                return False
        return True
    except Exception:
        async with _cond:
            _migrating.discard(sandbox_id)
        raise


async def release_migration(sandbox_id: str) -> None:
    async with _cond:
        _migrating.discard(sandbox_id)
        _cond.notify_all()
