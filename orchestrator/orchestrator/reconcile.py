"""Boot-time reconcile: rebuild the sandbox store from running containers.

The orchestrator's in-memory store is wiped on every restart; even with
the Postgres store, restarts have historically left the in-process view
out of sync with reality (orphaned containers, stale rows, etc.). This
module walks ``docker ps`` for our labeled containers on startup and
upserts every one into whatever ``SandboxStore`` is configured.

Net effect:

  - In-memory store: rehydrated to match the host's actual running set
    on every orchestrator boot. Restarts no longer wipe state.
  - Postgres store: idempotent reconciliation — any drift between the
    DB and Docker is corrected (DB row promoted to running if the
    container is alive but DB says stopped, marked stopped if the
    container is gone).

Container labels we rely on (set in ``sandbox_manager.create_sandbox``):

  - ``matrx.sandbox_id``     — required, the orchestrator's primary key
  - ``matrx.user_id``        — required
  - ``matrx.created_at``     — ISO8601, falls back to container.created
  - ``matrx.tier``           — optional ("ec2"|"hosted")
  - ``matrx.template``       — optional template name
  - ``matrx.template_version`` — optional

Anything missing falls back to a sensible default. We never delete
containers from the reconcile path — only update store state. Cleanup
of dead-but-still-present-in-DB rows happens on the next destroy or
expire path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from orchestrator.config import settings
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import SandboxStore

logger = logging.getLogger(__name__)

# A row in one of these statuses reflects a deliberate end-of-life. If a
# container for such a row is still alive, that's an orphan ("system says
# done but it's still running"), not something to resurrect to 'running'.
_TERMINAL_STATUSES = {"stopped", "expired", "failed"}


def _parse_created_at(value: str | None) -> datetime:
    """Parse the matrx.created_at label, fall back to "now" on bad data."""
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _docker_state_to_status(state: dict) -> SandboxStatus:
    """Map a Docker container's ``State`` to our SandboxStatus enum.

    We err on the side of "running" when there's any doubt — the agent
    cares whether they can talk to the container, not whether matrx_agent
    is fully booted yet. Diagnostics surface the difference for free.
    """
    if state.get("Running"):
        return SandboxStatus.RUNNING
    docker_status = (state.get("Status") or "").lower()
    if docker_status in {"created"}:
        return SandboxStatus.CREATING
    if docker_status in {"restarting"}:
        return SandboxStatus.STARTING
    if docker_status in {"removing", "dead", "exited"}:
        return SandboxStatus.STOPPED
    return SandboxStatus.STOPPED


def _persistence_volume_from_mounts(container_attrs: dict) -> str | None:
    """Pull the per-user named volume out of container mounts.

    Hosted-tier sandboxes mount ``matrx-user-<uid>`` at /home/agent. We
    care about that one specifically — anything else (Docker socket
    bind-mount, FUSE devices, etc.) is irrelevant to persistence.
    """
    for mount in container_attrs.get("Mounts", []) or []:
        name = mount.get("Name") or ""
        if name.startswith("matrx-user-"):
            return name
    return None


def _ssh_host_port(container_attrs: dict) -> int | None:
    ports = (container_attrs.get("NetworkSettings", {}) or {}).get("Ports") or {}
    bindings = ports.get("22/tcp")
    if bindings:
        try:
            return int(bindings[0].get("HostPort", 0)) or None
        except (TypeError, ValueError):
            return None
    return None


async def reconcile_from_docker(store: SandboxStore) -> dict:
    """Walk every container with ``matrx.sandbox_id`` and upsert into store.

    Returns a small summary dict for logging — counts and the list of
    sandbox IDs reconciled. Never raises; logs and continues on per-
    container errors.
    """
    summary = {
        "scanned": 0,
        "reconciled": 0,
        "skipped": 0,
        "reaped": 0,
        "failed": 0,
        "sandbox_ids": [],
    }

    try:
        # Lazy-import the docker client so this module doesn't pull docker
        # at import time (helps tests).
        from orchestrator.sandbox_manager import _get_docker_client
        client = _get_docker_client()
    except Exception as exc:
        logger.warning("Reconcile skipped: docker client unavailable: %s", exc)
        return summary

    try:
        containers = await asyncio.to_thread(
            lambda: client.containers.list(all=True, filters={"label": "matrx.sandbox_id"})
        )
    except Exception as exc:
        logger.warning("Reconcile skipped: docker.containers.list failed: %s", exc)
        return summary

    summary["scanned"] = len(containers)

    for container in containers:
        try:
            await asyncio.to_thread(container.reload)
            attrs = container.attrs or {}
            labels = (attrs.get("Config", {}) or {}).get("Labels") or {}
            sandbox_id = labels.get("matrx.sandbox_id")
            user_id = labels.get("matrx.user_id")

            if not sandbox_id or not user_id:
                summary["skipped"] += 1
                continue

            status = _docker_state_to_status(attrs.get("State", {}) or {})
            tier = labels.get("matrx.tier")
            # Honor the orchestrator's own tier setting as the default —
            # containers spawned by older code may not have the label.
            if not tier:
                tier = settings.host_tier or None

            # Skip cross-tier reconcile: a container whose tier doesn't
            # match this orchestrator was spawned by a sibling instance
            # (e.g. EC2 orchestrator labels persist if the container was
            # somehow migrated). Don't poach it.
            if settings.host_tier and tier and tier != settings.host_tier:
                summary["skipped"] += 1
                logger.debug(
                    "Reconcile skipped %s: tier=%s, host_tier=%s",
                    sandbox_id, tier, settings.host_tier,
                )
                continue

            # ── Warm-pool boxes ──────────────────────────────────────────────
            # An unclaimed warm box (matrx.warm_pool=1, no DB row) belongs to
            # the pool controller, not a user — don't reconcile it into the
            # store (its sentinel user_id would fail the auth.users FK anyway).
            # Once claimed it HAS a row; from then on the DB row's user_id is
            # authoritative over the (now-stale sentinel) label.
            is_warm = labels.get("matrx.warm_pool") == "1"
            existing_row = await store.get(sandbox_id)
            if is_warm and existing_row is None:
                summary["skipped"] += 1
                continue
            if existing_row is not None:
                user_id = existing_row.user_id

            # ── Respect the recorded end-of-life — never resurrect it ──
            # If the DB row was soft-deleted OR already moved to a terminal
            # status (stopped/expired/failed — a user/admin/reaper destroy),
            # but the container is somehow still running (e.g. the destroy
            # call's container.stop/remove silently failed, or it timed out
            # against a wedged orchestrator), this container is an ORPHAN.
            # Upserting its row back to "running" is exactly the "system says
            # done but it's still running" bug that left rows stuck in
            # 'running' for days. Reap the container instead, leaving the row
            # in its recorded terminal state. (A genuine resume sets the row
            # back to a live status synchronously, so it is never terminal
            # here.)
            lifecycle = await store.get_lifecycle(sandbox_id)
            if lifecycle and (
                lifecycle["deleted"] or lifecycle["status"] in _TERMINAL_STATUSES
            ):
                summary["reaped"] += 1
                logger.info(
                    "Reconcile reaping orphan %s: DB row is %s but container is "
                    "alive — destroying container, leaving row as-is.",
                    sandbox_id,
                    "soft-deleted" if lifecycle["deleted"] else lifecycle["status"],
                )
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except Exception as exc:
                    logger.warning("Reconcile: failed to reap orphan %s: %s", sandbox_id, exc)
                continue

            sandbox = SandboxResponse(
                sandbox_id=sandbox_id,
                user_id=user_id,
                status=status,
                container_id=container.id,
                # Docker proves runtime state, not user-authored metadata.
                # Preserve every metadata field already recorded in Postgres;
                # replacing config with {} here erased config for the entire
                # live fleet on every orchestrator restart.
                created_at=(
                    existing_row.created_at if existing_row is not None
                    else _parse_created_at(labels.get("matrx.created_at"))
                ),
                hot_path=(existing_row.hot_path if existing_row is not None else "/home/agent"),
                cold_path=(existing_row.cold_path if existing_row is not None else "/data/cold"),
                config=(existing_row.config if existing_row is not None else {}),
                ttl_seconds=(existing_row.ttl_seconds if existing_row is not None else 7200),
                tier=tier,
                template=labels.get("matrx.template"),
                template_version=labels.get("matrx.template_version"),
                labels=(existing_row.labels if existing_row is not None else None),
                ssh_port=_ssh_host_port(attrs),
                persistence_volume=(
                    _persistence_volume_from_mounts(attrs)
                    or (existing_row.persistence_volume if existing_row is not None else None)
                ),
            )

            await store.save(sandbox)
            summary["reconciled"] += 1
            summary["sandbox_ids"].append(sandbox_id)

        except Exception as exc:
            summary["failed"] += 1
            logger.warning(
                "Reconcile failed for container %s: %s",
                getattr(container, "id", "?")[:12], exc,
            )

    logger.info(
        "Reconcile complete: scanned=%d reconciled=%d skipped=%d reaped=%d failed=%d",
        summary["scanned"], summary["reconciled"], summary["skipped"],
        summary["reaped"], summary["failed"],
    )
    return summary


def _alive_container_ids(client, host_tier: str | None) -> set[str]:
    """Set of container IDs for THIS tier that are alive enough to keep their
    DB row in a live status.

    "Alive" = running, created, or restarting. Exited/dead/removing map to a
    terminal SandboxStatus, so those containers are treated as gone and their
    rows get reconciled to stopped. A still-booting container (created/
    restarting) is deliberately kept alive so we never stop a sandbox mid-boot.
    """
    alive: set[str] = set()
    containers = client.containers.list(
        all=True, filters={"label": "matrx.sandbox_id"}
    )
    for container in containers:
        try:
            container.reload()
            attrs = container.attrs or {}
            labels = (attrs.get("Config", {}) or {}).get("Labels") or {}
            tier = labels.get("matrx.tier") or host_tier
            # Only consider containers belonging to this orchestrator's tier.
            if host_tier and tier and tier != host_tier:
                continue
            status = _docker_state_to_status(attrs.get("State", {}) or {})
            if status in (
                SandboxStatus.RUNNING,
                SandboxStatus.CREATING,
                SandboxStatus.STARTING,
            ):
                alive.add(container.id)
        except Exception:
            # We can SEE this container in the list but failed to read its
            # state. Treat it as alive — never stop a row whose container
            # demonstrably exists just because a metadata read hiccupped.
            # (A cross-tier id added here is harmless: it won't match any of
            # this tier's rows.)
            try:
                alive.add(container.id)
            except Exception:
                pass
            continue
    return alive


async def reap_zombie_containers(store: SandboxStore) -> list[str]:
    """Destroy containers whose DB row records end-of-life but which are still
    alive on this host. Same rule as the boot-time orphan reap above, run from
    the reaper's 60s sweep — because ``expire_stale`` only returns rows on
    their FIRST transition past expires_at, a teardown that fails once (or an
    orchestrator restart between mark and destroy) used to leak the container
    until the next boot. 15 zombies accumulated exactly this way in July 2026.

    Tier-scoped, never raises, returns the reaped sandbox_ids.
    """
    reaped: list[str] = []
    host_tier = settings.host_tier or None
    try:
        from orchestrator.sandbox_manager import _get_docker_client
        client = _get_docker_client()
        containers = await asyncio.to_thread(
            lambda: client.containers.list(filters={"label": "matrx.sandbox_id"})
        )
    except Exception as exc:
        logger.warning("Zombie reap skipped: docker unavailable: %s", exc)
        return reaped

    for container in containers:
        try:
            labels = ((container.attrs or {}).get("Config", {}) or {}).get("Labels") or {}
            sandbox_id = labels.get("matrx.sandbox_id")
            if not sandbox_id:
                continue
            tier = labels.get("matrx.tier") or host_tier
            if host_tier and tier and tier != host_tier:
                continue
            if labels.get("matrx.warm_pool") == "1":
                continue
            lifecycle = await store.get_lifecycle(sandbox_id)
            if not lifecycle:
                continue
            if lifecycle["deleted"] or lifecycle["status"] in _TERMINAL_STATUSES:
                logger.info(
                    "Zombie reap: %s row is %s but container is alive — removing "
                    "container (volume preserved, row untouched).",
                    sandbox_id,
                    "soft-deleted" if lifecycle["deleted"] else lifecycle["status"],
                )
                await asyncio.to_thread(container.remove, force=True)
                reaped.append(sandbox_id)
        except Exception as exc:
            logger.warning(
                "Zombie reap failed for container %s: %s",
                getattr(container, "id", "?")[:12], exc,
            )
    return reaped


async def reconcile_liveness(store: SandboxStore) -> dict:
    """Tier-scoped liveness sweep: stop rows whose container vanished, refresh
    rows whose container is alive.

    The inverse of ``reconcile_from_docker``: that walks existing containers
    and upserts their rows; this walks live-status rows and reconciles them
    against the set of containers actually alive. Safe to call on boot and on
    the reaper's interval. Never raises. Returns ``{"stopped", "refreshed"}``.
    """
    summary = {"stopped": [], "refreshed": 0}

    host_tier = settings.host_tier or None
    if not host_tier:
        # Without a known tier we can't tell which rows in the shared
        # sandbox_instances table are "ours" — skip rather than risk marking a
        # sibling tier's healthy rows as stopped.
        return summary

    reconcile = getattr(store, "reconcile", None)
    if reconcile is None:
        return summary

    try:
        from orchestrator.sandbox_manager import _get_docker_client
        client = _get_docker_client()
    except Exception as exc:
        logger.warning("Liveness reconcile skipped: docker client unavailable: %s", exc)
        return summary

    try:
        alive_ids = await asyncio.to_thread(_alive_container_ids, client, host_tier)
    except Exception as exc:
        # If we can't enumerate containers, do NOT proceed — an empty/partial
        # alive set would wrongly stop healthy rows. Better to skip this tick.
        logger.warning("Liveness reconcile skipped: container list failed: %s", exc)
        return summary

    try:
        return await reconcile(alive_ids, tier=host_tier)
    except Exception as exc:
        logger.warning("Liveness reconcile failed: %s", exc)
        return summary


__all__ = ["reconcile_from_docker", "reconcile_liveness", "reap_zombie_containers"]
