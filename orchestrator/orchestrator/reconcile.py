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

import logging
from datetime import datetime, timezone

from orchestrator.config import settings
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import SandboxStore

logger = logging.getLogger(__name__)


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
        containers = client.containers.list(
            all=True,
            filters={"label": "matrx.sandbox_id"},
        )
    except Exception as exc:
        logger.warning("Reconcile skipped: docker.containers.list failed: %s", exc)
        return summary

    summary["scanned"] = len(containers)

    for container in containers:
        try:
            container.reload()
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

            # ── Respect the user's intent — never resurrect a deleted row ──
            # If the DB row was soft-deleted (user/admin destroyed it) but the
            # container is somehow still running (e.g. the destroy call timed
            # out against a wedged orchestrator), this container is an ORPHAN.
            # Resurrecting its row to "running" is exactly the "system says
            # destroyed but it's still running" bug. Reap the container
            # instead, leaving the DB row deleted.
            lifecycle = await store.get_lifecycle(sandbox_id)
            if lifecycle and lifecycle["deleted"]:
                summary["reaped"] += 1
                logger.info(
                    "Reconcile reaping orphan %s: DB row is soft-deleted but "
                    "container is alive — destroying container, leaving row deleted.",
                    sandbox_id,
                )
                try:
                    container.remove(force=True)
                except Exception as exc:
                    logger.warning("Reconcile: failed to reap orphan %s: %s", sandbox_id, exc)
                continue

            sandbox = SandboxResponse(
                sandbox_id=sandbox_id,
                user_id=user_id,
                status=status,
                container_id=container.id,
                created_at=_parse_created_at(labels.get("matrx.created_at")),
                hot_path="/home/agent",
                cold_path="/data/cold",
                config={},
                ttl_seconds=7200,  # default; on next heartbeat/extend the truth wins
                tier=tier,
                template=labels.get("matrx.template"),
                template_version=labels.get("matrx.template_version"),
                ssh_port=_ssh_host_port(attrs),
                persistence_volume=_persistence_volume_from_mounts(attrs),
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


__all__ = ["reconcile_from_docker"]
