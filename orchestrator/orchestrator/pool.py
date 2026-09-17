"""The warm pool is RETIRED (2026-09-17). This module is its tombstone + sweep.

WHY IT IS GONE
--------------
The pool pre-booted containers before anyone knew whose they were, so a warm
box carried a SENTINEL identity: ``USER_ID=<warm_pool_sentinel_user>``, no
``ORGANIZATION_ID``, no AI Dream URL or token. Claiming one was supposed to
hand it to a real person.

Two facts make that unfixable in the shape it had:

1. **Docker cannot change the environment of a running container.** Every
   process a warm box already started — the API daemon, the cloud-files
   watcher, the boot shells — has read ``os.environ`` by the time a claim
   arrives. The actor and organization a sandbox carries into AI Dream are not
   decoration: THE REQUEST CONTEXT IS CARRIED, NEVER REBUILT
   (``common-docs/policies/context-is-carried-never-rebuilt.md``), and a claim
   that patches identity in after boot means every already-running process
   keeps the sentinel one. That is precisely the class the law forbids, and its
   failure mode is writes landing in the wrong tenant.
2. **The claim path was already dead.** ``routes/sandboxes.py::claim_sandbox``
   skips the warm path whenever ``req.organization_id`` is set, and
   ``organization_id`` is REQUIRED on ``CreateSandboxRequest`` — so from the day
   the organization became mandatory, ``claim_warm`` was unreachable while
   ``pool_loop`` went on booting and retiring containers every 30 seconds
   forever. Nobody ever got a warm box; the fleet just paid for them.

So the pool is deleted rather than left running dead (no-legacy). ``POST
/sandboxes/claim`` still exists and still answers — it cold-creates, which is
what it already did in production for every real request.

WHAT REPLACING IT WOULD TAKE
----------------------------
A pre-warmed box that is genuinely claimable has to receive its identity
BEFORE anything in it reads the environment — i.e. warm the expensive parts
(image pull, layer warm, volume) and start the container's processes only at
claim time, or hand the identity to a box whose daemon starts on demand. That
is a design, not a patch; when someone builds it, it starts here.

WHAT THIS MODULE STILL DOES
---------------------------
Retirement has to be visible and has to clean up: ``retire_warm_pool()`` runs
once at boot, removes any leftover UNCLAIMED warm container (label
``matrx.warm_pool=1`` with no ``sandbox_instances`` row — nobody's box, and now
nothing will ever claim it), and SAYS SO. If the ``warm_pool_size`` /
``warm_pool_templates`` settings still carry a non-zero target, it says that
too, by name: a knob that no longer does anything must never look like it does.
"""

from __future__ import annotations

import asyncio
import logging

from orchestrator.knobs import knob_int, knob_str

logger = logging.getLogger(__name__)

WARM_LABEL = "matrx.warm_pool"

RETIRED_NOTICE = (
    "The warm pool is RETIRED: a pre-booted box cannot receive a user and an "
    "organization after boot (Docker cannot change a running container's "
    "environment), so every /sandboxes/claim cold-creates. See "
    "orchestrator/pool.py for the full reasoning."
)


def list_warm_containers(template: str | None = None) -> list:
    """Running containers still carrying the warm-pool label (optionally one
    template's). Used by the retirement sweep and by nothing else."""
    from orchestrator.sandbox_manager import _get_docker_client

    client = _get_docker_client()
    try:
        containers = client.containers.list(filters={"label": f"{WARM_LABEL}=1"})
    except Exception as exc:  # noqa: BLE001 — announced, never silent
        logger.warning("Warm-pool retirement: listing containers failed: %s", exc)
        return []
    out = []
    for c in containers:
        labels = (c.attrs.get("Config", {}) or {}).get("Labels") or {}
        if template and labels.get("matrx.template") != template:
            continue
        if c.status != "running":
            continue
        out.append(c)
    return out


async def _configured_target() -> str | None:
    """The warm-pool target the settings still ask for, or None.

    A settings read that fails is reported, never swallowed — an unreadable
    knob must not read as "nothing configured".
    """
    try:
        size = await knob_int("warm_pool_size")
        spec = (await knob_str("warm_pool_templates")).strip()
    except Exception as exc:  # noqa: BLE001
        return f"unreadable ({exc})"
    if spec:
        return spec
    if size:
        return f"warm_pool_size={size}"
    return None


async def retire_warm_pool() -> dict:
    """Announce the retirement and remove leftover unclaimed warm containers.

    A warm container with no ``sandbox_instances`` row belongs to nobody and
    nothing will ever claim it, so it is removed. A warm-LABELLED container
    that DOES have a row was claimed while the pool still worked — it belongs
    to a user and is never touched.
    """
    summary = {"removed": [], "kept_claimed": 0, "configured_target": None}

    target = await _configured_target()
    summary["configured_target"] = target
    if target is not None:
        logger.warning(
            "WARM POOL SETTINGS IGNORED: infrastructure.sandbox still asks for "
            "%s, and nothing will act on it. %s Set the setting to 0/empty so "
            "the fleet's configuration stops describing a system that is gone.",
            target,
            RETIRED_NOTICE,
        )

    from orchestrator.sandbox_manager import _get_store

    store = _get_store()
    try:
        containers = await asyncio.to_thread(list_warm_containers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Warm-pool retirement sweep could not list containers: %s", exc)
        return summary

    for c in containers:
        labels = (c.attrs.get("Config", {}) or {}).get("Labels") or {}
        sid = labels.get("matrx.sandbox_id")
        if not sid:
            continue
        try:
            if await store.get(sid) is not None:
                summary["kept_claimed"] += 1
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Warm-pool retirement: store.get(%s) failed; leaving the "
                "container alone rather than removing a box that may be owned: %s",
                sid, exc,
            )
            continue
        try:
            await asyncio.to_thread(c.remove, force=True)
            summary["removed"].append(sid)
            logger.warning(
                "Warm-pool retirement: removed unclaimed warm container %s "
                "(no sandbox_instances row; the pool that made it is gone)", sid,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Warm-pool retirement: could not remove unclaimed warm container %s: %s",
                sid, exc,
            )

    if summary["removed"] or summary["kept_claimed"]:
        logger.info(
            "Warm-pool retirement sweep: removed %d unclaimed, left %d claimed box(es) alone",
            len(summary["removed"]), summary["kept_claimed"],
        )
    return summary


__all__ = ["retire_warm_pool", "list_warm_containers", "WARM_LABEL", "RETIRED_NOTICE"]
