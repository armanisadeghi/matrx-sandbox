"""Binding hooks run in the background; a token mint never waits on them.

THE INCIDENT (2026-10-17). The admin development box got NO sandbox tools in
chat: the browser's token mint has a ~6.75 s budget, and the orchestrator's
``POST /sandboxes/{id}/access-tokens`` took 71-78 s because the SessionStart
hooks (SDK refresh, vault env refresh, development repo sync) ran
SYNCHRONOUSLY inside it. The SDK refresh alone was a 65 s install that failed,
rolled back, and was repeated on every binding.

THE PRIMITIVE. Each box gets at most ONE in-flight preparation task
(single-flight, held in a module dict so it is never garbage collected). A
caller waits for it for at most :data:`HOOK_WAIT_SECONDS`; when it has not
finished, the caller gets the partial report — every hook that is still running
says ``status: "in_progress"`` — and the task keeps running and finishes on its
own. A later binding that arrives while it still runs joins the same task
instead of starting a second one.

The wait is a constant, not a knob: it is a property of the mint contract (the
caller's budget is seconds), not an organization's preference.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: How long a token mint waits for the box's connection hooks before it answers
#: with whatever has finished. Well inside the browser mint's 6.75 s budget.
HOOK_WAIT_SECONDS = 2.0

#: sandbox_id -> (task, partial report the task fills in as each hook finishes)
_inflight: dict[str, tuple[asyncio.Task, dict]] = {}


def in_progress(hook: str, **extra) -> dict:
    out = {
        "hook": hook,
        "status": "in_progress",
        "reason": "still running in the background; the token was issued without waiting",
    }
    out.update(extra)
    return out


def _finished(sandbox_id: str, task: asyncio.Task) -> None:
    current = _inflight.get(sandbox_id)
    if current is not None and current[0] is task:
        _inflight.pop(sandbox_id, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "CONNECTION HOOKS crashed in the background for %s: %s: %s",
            sandbox_id, type(exc).__name__, exc,
        )


async def run_bounded(
    sandbox_id: str,
    run: Callable[[dict], Awaitable[dict | None]],
    *,
    wait: float | None = None,
) -> tuple[bool, dict | None, dict]:
    """Start (or join) the box's preparation and wait at most ``wait`` seconds.

    ``run(partial)`` performs the hooks, writing each hook's result into
    ``partial`` as it lands, and returns the full report.

    Returns ``(finished, report, partial)``: ``report`` is the full report when
    ``finished``; otherwise use ``partial``.
    """
    budget = HOOK_WAIT_SECONDS if wait is None else wait
    entry = _inflight.get(sandbox_id)
    if entry is None or entry[0].done():
        partial: dict = {}
        task = asyncio.create_task(run(partial), name=f"connection-hooks:{sandbox_id}")
        _inflight[sandbox_id] = (task, partial)
        task.add_done_callback(lambda t, sid=sandbox_id: _finished(sid, t))
        entry = (task, partial)
    task, partial = entry
    try:
        report = await asyncio.wait_for(asyncio.shield(task), timeout=budget)
    except asyncio.TimeoutError:
        logger.warning(
            "CONNECTION HOOKS for %s still running after %.1fs; issuing the token now, "
            "they finish in the background (done so far: %s)",
            sandbox_id, budget, sorted(partial) or "nothing",
        )
        return False, None, partial
    except Exception as exc:  # noqa: BLE001 — a hook crash never blocks a mint
        logger.exception("CONNECTION HOOKS failed for %s", sandbox_id)
        return True, {**partial, "status": "failed", "summary": f"{type(exc).__name__}"}, partial
    return True, report, partial


def is_running(sandbox_id: str) -> bool:
    entry = _inflight.get(sandbox_id)
    return entry is not None and not entry[0].done()


async def drain() -> None:
    """Tests: cancel and await every background preparation."""
    entries = list(_inflight.values())
    _inflight.clear()
    for task, _partial in entries:
        task.cancel()
    for task, _partial in entries:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
