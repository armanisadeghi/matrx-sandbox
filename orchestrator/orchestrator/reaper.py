"""Background reaper — the missing half of the sandbox lifecycle.

The bug this fixes: ``mark_expired`` existed in the store but NOTHING ever
called it. A sandbox would hit ``expires_at``, and... nothing happened.
The container kept running forever, the FE computed "expired" client-side
and blocked the user, the data was never flushed, and orphan containers
piled up (the user accumulated 17+ this way).

The reaper closes that loop. On a fixed interval it:

  1. Asks the store for sandboxes past ``expires_at`` that are still in a
     live status (``ready``/``running``/``starting``) and not deleted.
  2. For each, runs a GRACEFUL teardown: ``destroy_sandbox(reason="expired")``
     sends SIGTERM, the in-container ``shutdown.sh`` trap runs the final
     cloud-files up-sync, the container is removed, and the per-user
     volume is PRESERVED.
  3. Marks the row ``expired`` — a resumable terminal state, distinct from
     ``deleted`` (which means "gone, volume wipe-eligible").

The user's mental model, now actually implemented:
  - Get a unit for 2h.
  - At 2h: flush data → tear down container → keep volume → mark expired.
  - Want it back: ``POST /sandboxes/{id}/resume`` spawns a fresh container
    (latest image) on the same volume.
  - Permanent = ``ttl_seconds`` huge / never reaped; same machinery.

Idempotent and self-healing: a teardown failure for one sandbox is logged
and the loop moves on; the next tick retries.
"""

from __future__ import annotations

import asyncio
import logging
import time

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# How often to sweep for expired sandboxes. 60s is responsive enough that
# "at 2h it tears down" feels accurate without hammering Docker/Postgres.
REAP_INTERVAL_SECONDS = 60

# Auto-migrate backoff. A drifted box that FAILS to migrate (e.g. its target
# image is missing/broken) would otherwise be retried every 60s forever —
# Docker load + log spam with no progress. Track consecutive failing passes and
# defer the next attempt with exponential backoff (capped at 1h). Busy/deferred
# boxes do NOT trigger backoff — they legitimately retry every sweep.
_MIGRATE_BACKOFF_CAP_SECONDS = 3600
_migrate_backoff = {"next_attempt": 0.0, "fails": 0}


async def _reap_once() -> dict:
    """Single sweep. Returns a summary dict for logging. Never raises."""
    summary = {
        "expired_found": 0, "torn_down": 0, "failed": 0, "sandbox_ids": [],
        "liveness_stopped": 0, "liveness_refreshed": 0,
    }
    try:
        from orchestrator.models import SandboxStatus
        from orchestrator.sandbox_manager import _get_store, destroy_sandbox
        store = _get_store()
    except Exception as exc:
        logger.warning("Reaper: store unavailable this tick: %s", exc)
        return summary

    # expire_stale flips status→expired + stamps stopped_at/stop_reason for
    # every row past expires_at (and not soft-deleted), and returns the
    # affected sandbox_ids. We then physically tear down each container.
    # Marking first means that even if a teardown fails, the row already
    # reflects "expired" so the FE stops showing it as usable.
    try:
        expired_ids = await store.expire_stale()
    except Exception as exc:
        logger.warning("Reaper: expire_stale failed this tick: %s", exc)
        return summary

    summary["expired_found"] = len(expired_ids)
    for sandbox_id in expired_ids:
        try:
            # graceful=True → SIGTERM → in-container shutdown.sh trap runs
            # the final cloud-files up-sync ("triple-check we didn't miss
            # anything"). final_status=EXPIRED keeps the row resumable and
            # distinct from a user-stopped sandbox; the per-user volume is
            # preserved either way.
            ok = await destroy_sandbox(
                sandbox_id,
                graceful=True,
                reason="expired",
                final_status=SandboxStatus.EXPIRED,
            )
            if ok:
                summary["torn_down"] += 1
                summary["sandbox_ids"].append(sandbox_id)
            else:
                summary["failed"] += 1
        except Exception as exc:
            summary["failed"] += 1
            logger.warning("Reaper: teardown of expired %s failed: %s", sandbox_id, exc)

    # Zombie reap every tick: containers whose row already records end-of-life
    # (a prior destroy failed, or an orchestrator restart landed between the
    # expire-mark and the teardown). expire_stale() never returns those rows
    # again, so without this they leak until the next orchestrator boot.
    try:
        from orchestrator.reconcile import reap_zombie_containers
        zombies = await reap_zombie_containers(store)
        summary["zombies_reaped"] = len(zombies)
    except Exception as exc:
        logger.warning("Reaper: zombie reap failed this tick: %s", exc)

    # Retention sweep: rows finished (stopped/expired/failed) for more than
    # terminal_retention_days get soft-deleted so every UI's default list
    # forgets them. Resumable until then; the row + per-user volume survive.
    if settings.terminal_retention_days > 0:
        try:
            purged = await store.purge_terminal_older_than(settings.terminal_retention_days)
            summary["retention_purged"] = len(purged)
            if purged:
                logger.info(
                    "Retention sweep: soft-deleted %d finished sandbox(es) older "
                    "than %dd: %s",
                    len(purged), settings.terminal_retention_days, ", ".join(purged),
                )
        except Exception as exc:
            logger.warning("Reaper: retention sweep failed this tick: %s", exc)

    # Liveness reconcile every tick — two jobs the TTL sweep above can't do:
    #   1. Mark rows whose container has VANISHED as stopped within ~60s,
    #      instead of waiting for the next orchestrator reboot. These are the
    #      rows that otherwise sit stuck in 'running' (the watchdog offenders).
    #   2. Refresh updated_at for rows whose container is still alive, so a
    #      healthy long-lived sandbox keeps a fresh timestamp and never trips
    #      the persistence watchdog's max-age SLA just for running a long time.
    # Tier-scoped and never raises; failures are logged and the loop continues.
    try:
        from orchestrator.reconcile import reconcile_liveness
        live = await reconcile_liveness(store)
        summary["liveness_stopped"] = len(live["stopped"])
        summary["liveness_refreshed"] = live["refreshed"]
    except Exception as exc:
        logger.warning("Reaper: liveness reconcile failed this tick: %s", exc)

    # Version-drift alarm (zero-drift system, Phase 1). Tier-scoped, never
    # raises. drift_summary() logs loudly when any live box is stale; we also
    # surface the count in the sweep summary so it shows in the periodic line.
    try:
        from orchestrator.sandbox_manager import _get_docker_client
        from orchestrator.versioning import drift_summary
        drift = drift_summary(_get_docker_client())
        summary["drifted"] = drift["drifted"]
    except Exception as exc:
        logger.warning("Reaper: drift check failed this tick: %s", exc)

    # Opt-in rolling auto-migration: when enabled, migrate a few drifted boxes
    # each sweep (busy ones deferred to the next sweep). Off by default.
    if settings.auto_migrate and summary.get("drifted"):
        now = time.monotonic()
        if now < _migrate_backoff["next_attempt"]:
            summary["auto_migrate_deferred_until"] = _migrate_backoff["next_attempt"]
        else:
            try:
                from orchestrator.migrate import migrate_all_drifted
                mig = await migrate_all_drifted(store=store, max_per_pass=settings.migrate_max_per_pass)
                summary["auto_migrated"] = len(mig.get("migrated", []))
                # Back off only on genuine FAILURES (broken/missing image), not
                # on busy 'deferred' boxes which should keep retrying each sweep.
                if mig.get("failed") or mig.get("error"):
                    _migrate_backoff["fails"] += 1
                    delay = min(
                        REAP_INTERVAL_SECONDS * (2 ** _migrate_backoff["fails"]),
                        _MIGRATE_BACKOFF_CAP_SECONDS,
                    )
                    _migrate_backoff["next_attempt"] = now + delay
                    logger.warning(
                        "Reaper: auto-migrate had %d failure(s); backing off %ds",
                        len(mig.get("failed", [])) or 1, delay,
                    )
                elif mig.get("migrated"):
                    # Progress — clear the backoff.
                    _migrate_backoff["fails"] = 0
                    _migrate_backoff["next_attempt"] = 0.0
            except Exception as exc:
                _migrate_backoff["fails"] += 1
                delay = min(
                    REAP_INTERVAL_SECONDS * (2 ** _migrate_backoff["fails"]),
                    _MIGRATE_BACKOFF_CAP_SECONDS,
                )
                _migrate_backoff["next_attempt"] = time.monotonic() + delay
                logger.warning("Reaper: auto-migrate raised: %s (backing off %ds)", exc, delay)

    if (summary["expired_found"] or summary["liveness_stopped"]
            or summary.get("drifted") or summary.get("zombies_reaped")):
        logger.info(
            "Reaper sweep: expired_found=%d torn_down=%d failed=%d "
            "liveness_stopped=%d liveness_refreshed=%d drifted=%d zombies_reaped=%d",
            summary["expired_found"], summary["torn_down"], summary["failed"],
            summary["liveness_stopped"], summary["liveness_refreshed"],
            summary.get("drifted", 0), summary.get("zombies_reaped", 0),
        )
    return summary


async def reaper_loop(stop_event: asyncio.Event) -> None:
    """Run ``_reap_once`` every REAP_INTERVAL_SECONDS until stop_event set.

    Mounted from the FastAPI lifespan. The stop_event lets the lifespan
    shut the loop down cleanly on app teardown.
    """
    logger.info("Reaper started (interval=%ds)", REAP_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            await _reap_once()
        except Exception as exc:  # belt-and-suspenders; _reap_once shouldn't raise
            logger.warning("Reaper tick errored (continuing): %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=REAP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("Reaper stopped")


__all__ = ["reaper_loop", "_reap_once", "REAP_INTERVAL_SECONDS"]
