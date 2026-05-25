"""Warm pool — pre-booted sandboxes ready to CLAIM, for chat-speed launch.

The user's spec: "2 warm instances ready at any moment; when one is launched,
prepare another." A cold create pays container start + (for heavier tiers) an
image pull; a CLAIM just adopts an already-running, daemon-ready box, so launch
drops to seconds — fast enough to fire from a chat window.

The hard part, designed around here: a box is warmed BEFORE we know who it's
for, and Docker labels are immutable on a running container. So:

  - A warm box boots with a PRE-GENERATED ``sandbox_id`` baked into its labels
    (``matrx.sandbox_id``), a ``matrx.warm_pool=1`` marker, and the sentinel
    user (``settings.warm_pool_sentinel_user``). It has NO ``sandbox_instances``
    row and NO per-user volume — it belongs to nobody yet.
  - **Claim** creates the DB row (reusing that pre-generated sandbox_id) with
    the REAL user_id + a fresh TTL, hydrates the user's memory into it, and
    fires a replenish. From that moment the DB row — not the stale sentinel
    label — is authoritative for ownership.
  - Boot **reconcile** cooperates: it SKIPS warm boxes with no DB row (the pool
    owns them) and, for any box that does have a row, trusts the row's user_id
    over the label (so a claimed warm box isn't reverted to the sentinel).

Disabled unless ``MATRX_WARM_POOL_SIZE > 0``. Hosted tier sets it to 2.

EC2 note: this manages warm CONTAINERS on whatever host the orchestrator runs.
On EC2 the bigger latency is instance boot + image pull; pairing this with a
warm INSTANCE (image pre-pulled / baked AMI) is the remaining infra step
(docs/EC2_LIGHTWEIGHT_BOX.md §8 step 5). The claim/replenish logic is identical.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from orchestrator.config import settings
from orchestrator.models import SandboxResponse, SandboxStatus

logger = logging.getLogger(__name__)

WARM_LABEL = "matrx.warm_pool"
POOL_INTERVAL_SECONDS = 30


def _warm_template() -> str:
    return settings.warm_pool_template or "slim"


def _warm_targets() -> list[tuple[str, int]]:
    """The (template, count) pairs to keep warmed.

    Parsed from ``MATRX_WARM_POOL_TEMPLATES`` ("slim:1,aidream:1"); a bare name
    uses ``warm_pool_size``. Falls back to the single-template config when the
    multi-template spec is empty.
    """
    spec = (settings.warm_pool_templates or "").strip()
    if spec:
        targets: list[tuple[str, int]] = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            name, sep, cnt = part.partition(":")
            name = name.strip()
            if not name:
                continue
            try:
                n = int(cnt) if sep else settings.warm_pool_size
            except ValueError:
                n = settings.warm_pool_size
            if n > 0:
                targets.append((name, n))
        if targets:
            return targets
    if settings.warm_pool_size > 0:
        return [(_warm_template(), settings.warm_pool_size)]
    return []


def _pool_enabled() -> bool:
    return bool(_warm_targets())


def _current_image_id(template: str) -> str | None:
    """Docker image ID currently backing ``template`` — used to detect warm
    boxes booted from a now-superseded image (the version-refresh signal)."""
    from orchestrator.sandbox_manager import _get_docker_client
    from orchestrator.routes.templates import resolve_template_image
    tag = resolve_template_image(template) or settings.sandbox_image
    try:
        return _get_docker_client().images.get(tag).id
    except Exception as exc:
        logger.warning("Pool: could not resolve current image for template=%s: %s", template, exc)
        return None


async def _retire_stale_warm(template: str, current_image_id: str | None) -> int:
    """Remove UNCLAIMED warm boxes for ``template`` whose image no longer matches
    ``current_image_id`` (i.e. the template image was rebuilt). Never touches a
    claimed box. Returns how many were retired."""
    if not current_image_id:
        return 0
    retired = 0
    for c in await _unclaimed_warm(template):
        try:
            if c.image.id != current_image_id:
                sid = (c.attrs.get("Config", {}) or {}).get("Labels", {}).get("matrx.sandbox_id", c.id[:12])
                c.remove(force=True)
                retired += 1
                logger.info("Pool: retired stale warm %s (template=%s, image upgraded)", sid, template)
        except Exception as exc:
            logger.warning("Pool: failed to retire stale warm box (%s): %s", template, exc)
    return retired


def list_warm_containers(template: str | None = None) -> list:
    """Running, unclaimed warm containers (optionally filtered by template)."""
    from orchestrator.sandbox_manager import _get_docker_client
    client = _get_docker_client()
    try:
        containers = client.containers.list(filters={"label": f"{WARM_LABEL}=1"})
    except Exception as exc:
        logger.warning("Pool: list warm containers failed: %s", exc)
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


def _warm_run_container(template: str):
    """Boot one unclaimed warm container. Returns the docker container or None.

    Minimal sibling of ``create_sandbox``'s run block: warm labels, sentinel
    user, NO DB row, NO per-user volume (slim is git-persistence; a warm box
    has no owner to mount for).
    """
    from orchestrator.sandbox_manager import _get_docker_client
    from orchestrator.routes.templates import resolve_template_image

    client = _get_docker_client()
    sandbox_id = f"sbx-{uuid.uuid4().hex[:12]}"
    image = resolve_template_image(template) or settings.sandbox_image
    created_at = datetime.now(timezone.utc)

    env = {
        "SANDBOX_ID": sandbox_id,
        "USER_ID": settings.warm_pool_sentinel_user,
        "HOT_PATH": "/home/agent",
        "MATRX_TIER": settings.host_tier or "",
        "SHUTDOWN_TIMEOUT_SECONDS": str(settings.shutdown_timeout_seconds),
    }
    if template:
        env["SANDBOX_TEMPLATE"] = template

    try:
        container = client.containers.run(
            image=image,
            name=sandbox_id,
            detach=True,
            environment=env,
            cpu_period=100000,
            cpu_quota=int(settings.container_cpu_limit * 100000),
            mem_limit=settings.container_memory_limit,
            cap_add=["SYS_ADMIN"],
            devices=["/dev/fuse"],
            cap_drop=[],
            ports={"22/tcp": None},
            network=settings.docker_network,
            extra_hosts={"host.docker.internal": "host-gateway"},
            labels={
                "matrx.sandbox_id": sandbox_id,
                "matrx.user_id": settings.warm_pool_sentinel_user,
                "matrx.created_at": created_at.isoformat(),
                WARM_LABEL: "1",
                "matrx.template": template,
                **({"matrx.tier": settings.host_tier} if settings.host_tier else {}),
            },
            restart_policy={"Name": "no", "MaximumRetryCount": 0},
        )  # type: ignore[call-overload]
        logger.info("Pool: warmed %s (image=%s, template=%s)", sandbox_id, image, template)
        return container
    except Exception as exc:
        logger.warning("Pool: failed to warm a %s container: %s", template, exc)
        return None


async def _unclaimed_warm(template: str) -> list:
    """Warm containers that are still UNCLAIMED.

    A claimed warm box keeps its (immutable) ``warm_pool=1`` label forever, so
    the label alone can't tell claimed from unclaimed — we also require that no
    ``sandbox_instances`` row exists for its sandbox_id. This is what stops a
    box being double-claimed and what makes replenishment count correctly.
    """
    from orchestrator.sandbox_manager import _get_store
    store = _get_store()
    out = []
    for c in list_warm_containers(template):
        labels = (c.attrs.get("Config", {}) or {}).get("Labels") or {}
        sid = labels.get("matrx.sandbox_id")
        if not sid:
            continue
        try:
            if await store.get(sid) is None:
                out.append(c)
        except Exception as exc:
            logger.warning("Pool: store.get(%s) failed while counting warm: %s", sid, exc)
    return out


async def ensure_warm_pool() -> dict:
    """For every (template, count) target: retire warm boxes whose image was
    superseded (version-refresh), then top the template up to its count."""
    summary = {"per_template": {}, "warmed": 0, "retired": 0}
    for template, target in _warm_targets():
        retired = await _retire_stale_warm(template, _current_image_id(template))
        have = len(await _unclaimed_warm(template))  # post-retire count of fresh boxes
        warmed = 0
        for _ in range(max(0, target - have)):
            if _warm_run_container(template) is not None:
                warmed += 1
        summary["per_template"][template] = {"target": target, "have": have, "warmed": warmed, "retired": retired}
        summary["warmed"] += warmed
        summary["retired"] += retired
    return summary


async def claim_warm(
    user_id: str,
    template: str | None = None,
    ttl_seconds: int | None = None,
) -> SandboxResponse | None:
    """Adopt a warm box for ``user_id``. Returns the SandboxResponse, or None
    if no warm box of the right template is available (caller cold-creates).
    """
    template = template or _warm_template()
    if not _pool_enabled():
        return None

    candidates = await _unclaimed_warm(template)
    if not candidates:
        return None

    from orchestrator.sandbox_manager import _get_store, _get_docker_client, _proxy_url_for

    store = _get_store()
    container = candidates[0]
    container.reload()
    labels = (container.attrs.get("Config", {}) or {}).get("Labels") or {}
    sandbox_id = labels.get("matrx.sandbox_id")
    if not sandbox_id:
        return None

    # Read the SSH host port the warm box already mapped.
    ssh_port = None
    try:
        bindings = (container.attrs["NetworkSettings"]["Ports"] or {}).get("22/tcp")
        if bindings:
            ssh_port = int(bindings[0]["HostPort"])
    except (KeyError, TypeError, ValueError):
        pass

    sandbox = SandboxResponse(
        sandbox_id=sandbox_id,
        user_id=user_id,
        status=SandboxStatus.READY,
        container_id=container.id,
        created_at=datetime.now(timezone.utc),
        hot_path="/home/agent",
        cold_path="/data/cold",
        config={"warm_claimed": True},
        ttl_seconds=ttl_seconds or settings.max_session_duration_seconds,
        tier=settings.host_tier or None,
        template=template,
        ssh_port=ssh_port,
        proxy_url=_proxy_url_for(sandbox_id),
    )
    # Create the row, then stamp expires_at explicitly — the DB's auto-expire
    # trigger only fires on UPDATE into ready/running, but we INSERT as ready,
    # so without this expire_stale would never reap a claimed warm box.
    await store.save(sandbox)
    try:
        new_expires = await store.extend_ttl(sandbox_id, sandbox.ttl_seconds)
        if new_expires:
            sandbox.expires_at = new_expires
    except Exception as exc:
        logger.warning("Pool: claim could not set expires_at for %s: %s", sandbox_id, exc)

    # Hydrate this user's central memory into the freshly-claimed box.
    try:
        from orchestrator.memory_sync import hydrate_memory_into_container
        await hydrate_memory_into_container(container, user_id, store)
    except Exception as exc:
        logger.warning("Pool: memory hydrate on claim failed for %s: %s", sandbox_id, exc)

    logger.info("Pool: claimed warm %s for user %s (template=%s)", sandbox_id, user_id, template)

    # Replenish in the background so the pool is topped up for the next launch.
    asyncio.create_task(_replenish_async())
    return sandbox


async def _replenish_async() -> None:
    try:
        await ensure_warm_pool()
    except Exception as exc:
        logger.warning("Pool: async replenish failed: %s", exc)


async def pool_loop(stop_event: asyncio.Event) -> None:
    """Maintain the warm pool on an interval until stop_event is set."""
    if not _pool_enabled():
        logger.info("Warm pool disabled (set MATRX_WARM_POOL_SIZE or MATRX_WARM_POOL_TEMPLATES)")
        return
    logger.info(
        "Warm pool started (targets=%s, interval=%ds)",
        _warm_targets(), POOL_INTERVAL_SECONDS,
    )
    while not stop_event.is_set():
        try:
            await ensure_warm_pool()
        except Exception as exc:
            logger.warning("Pool tick errored (continuing): %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POOL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("Warm pool stopped")


__all__ = ["pool_loop", "ensure_warm_pool", "claim_warm", "list_warm_containers", "WARM_LABEL"]
