"""Periodic checkpoint daemon — bounds data loss on hard kill to ~5 minutes.

Runs as an asyncio background task inside the matrx_agent FastAPI app. Every
``interval_seconds`` it acquires a lock under ``.matrx/locks/checkpoint`` and
writes a fresh ``session.json`` (without auto-stash; that's shutdown-only).

Lock prevents overlap with the explicit ``/internal/shutdown`` writer. If the
checkpoint task is wedged (e.g. on a slow ``du``), the shutdown writer skips
gracefully + records the conflict in the manifest's ``transient_things_we_could_not_save``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from matrx_agent.persistence.manifest import (
    LOCK_DIR,
    MATRX_DIR,
    collect_manifest,
    write_manifest,
)

logger = logging.getLogger(__name__)

CHECKPOINT_LOCK = LOCK_DIR / "checkpoint"
DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes — see PERSISTENCE_PLAN.md §4.6


class CheckpointDaemon:
    """Owns the periodic checkpoint loop. Cancellable on app shutdown."""

    def __init__(self, interval_seconds: int = DEFAULT_INTERVAL_SECONDS):
        self.interval = max(30, interval_seconds)
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        """Begin the loop. Idempotent — calling twice is a no-op."""
        if self._task is not None and not self._task.done():
            return
        MATRX_DIR.mkdir(parents=True, exist_ok=True)
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="matrx-checkpoint")
        logger.info("Checkpoint daemon started (interval=%ds)", self.interval)

    async def stop(self) -> None:
        """Stop the loop and wait for it to exit. Safe to call from app shutdown."""
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        logger.info("Checkpoint daemon stopped")

    async def _run(self) -> None:
        # Initial 60 s grace so we don't fight the startup hot-sync.
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=60)
            return  # stopped during grace period
        except asyncio.TimeoutError:
            pass

        while not self._stopped.is_set():
            try:
                await asyncio.to_thread(self._checkpoint_once)
            except Exception as e:  # noqa: BLE001 — never let one tick kill the loop
                logger.warning("Checkpoint failed: %s", e)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
                return  # stop signal arrived
            except asyncio.TimeoutError:
                continue

    @staticmethod
    def _checkpoint_once() -> None:
        """Single checkpoint write — runs in a thread so manifest collection
        (which shells out to ``git`` + ``du``) doesn't block the asyncio loop.
        """
        # Best-effort lock so /internal/shutdown can detect concurrent writes.
        # We don't want a true lock here — if shutdown overrides us mid-tick,
        # that's actually fine; the shutdown manifest is more accurate.
        marker = CHECKPOINT_LOCK
        try:
            marker.write_text(str(int(time.time())))
            manifest = collect_manifest(graceful=False)
            write_manifest(manifest)
        finally:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
