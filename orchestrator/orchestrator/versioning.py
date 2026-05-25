"""Sandbox image version + drift detection — Phase 1 of the zero-drift system.

A box has *drifted* when the image it is currently RUNNING differs from the
CURRENT image for its template tag. We detect that primarily by **image ID**
(exact, and works on boxes that are already running — no rebuild required), and
surface the human-readable baked version (``MATRX_IMAGE_VERSION``, see
sandbox-image/Dockerfile) for operators and for the Phase-2 resume self-check.

Tier-scoped by construction: each orchestrator only sees its own host's
containers through the Docker socket, so the ec2 orchestrator reports ec2 drift
and the hosted one reports hosted drift. The drift report from each is therefore
authoritative for its own tier.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# The Dockerfile default when an image was NOT built through sandbox-image/build.sh.
UNVERSIONED = "dev"


def _version_from_image_attrs(image_attrs: dict | None) -> str | None:
    cfg = (image_attrs or {}).get("Config") or {}
    labels = cfg.get("Labels") or {}
    v = labels.get("com.aimatrx.sandbox.version")
    if v:
        return v
    for env in cfg.get("Env") or []:
        if env.startswith("MATRX_IMAGE_VERSION="):
            return env.split("=", 1)[1] or None
    return None


def _version_from_container(container) -> str | None:
    """Read the baked version off the container's own config — survives even if
    the image the container was created from was later untagged/removed."""
    cfg = (getattr(container, "attrs", None) or {}).get("Config") or {}
    for env in cfg.get("Env") or []:
        if env.startswith("MATRX_IMAGE_VERSION="):
            return env.split("=", 1)[1] or None
    return None


@dataclass
class CurrentImage:
    template: str | None
    tag: str
    image_id: str | None
    version: str | None
    available: bool


def current_image(client, template: str | None) -> CurrentImage:
    """Resolve the current image (id + baked version) for a template tag."""
    from orchestrator.routes.templates import resolve_template_image

    tag = resolve_template_image(template) or settings.sandbox_image
    try:
        img = client.images.get(tag)
        return CurrentImage(template, tag, img.id, _version_from_image_attrs(img.attrs), True)
    except Exception:
        # Tag not present on this host — we can't compare, so we won't claim drift.
        return CurrentImage(template, tag, None, None, False)


@dataclass
class BoxDrift:
    sandbox_id: str | None
    template: str | None
    tier: str | None
    running_image_id: str | None
    running_version: str | None
    current_image_id: str | None
    current_version: str | None
    current_image_available: bool
    drifted: bool
    reason: str


def compute_drift(client) -> list[BoxDrift]:
    """Compare every live, claimed box on this host to the current image for its
    template. Warm/unclaimed boxes are skipped — the warm-pool refresher already
    retires those on an image change."""
    try:
        containers = client.containers.list(filters={"label": "matrx.sandbox_id"})
    except Exception as exc:  # docker unreachable — report nothing rather than crash
        logger.warning("drift: containers.list failed: %s", exc)
        return []

    cache: dict[str, CurrentImage] = {}
    out: list[BoxDrift] = []
    for c in containers:
        labels = c.labels or {}
        if labels.get("matrx.warm_pool") == "1":
            continue
        template = labels.get("matrx.template")
        tier = labels.get("matrx.tier") or settings.host_tier or None
        running_image_id = (getattr(c, "attrs", None) or {}).get("Image")
        running_version = _version_from_container(c)

        key = template or ""
        cur = cache.get(key)
        if cur is None:
            cur = current_image(client, template)
            cache[key] = cur

        if not cur.available:
            drifted, reason = False, f"current image {cur.tag!r} not present on host — cannot compare"
        elif running_image_id and cur.image_id and running_image_id != cur.image_id:
            drifted, reason = True, "running image id differs from current tag"
        elif running_version == UNVERSIONED or running_version is None:
            drifted, reason = False, "running on an unversioned (dev) image — id matches current, no drift"
        else:
            drifted, reason = False, "up to date"

        out.append(
            BoxDrift(
                sandbox_id=labels.get("matrx.sandbox_id"),
                template=template,
                tier=tier,
                running_image_id=running_image_id,
                running_version=running_version,
                current_image_id=cur.image_id,
                current_version=cur.version,
                current_image_available=cur.available,
                drifted=drifted,
                reason=reason,
            )
        )
    return out


def drift_summary(client, *, log: bool = True) -> dict:
    """Drift report for this orchestrator's tier. Logs loudly when any box is
    stale (the Phase-1 alarm) so drift is never silent."""
    boxes = compute_drift(client)
    stale = [b for b in boxes if b.drifted]
    if log and stale:
        logger.warning(
            "SANDBOX VERSION DRIFT [tier=%s]: %d/%d live boxes are stale: %s",
            settings.host_tier or "?",
            len(stale),
            len(boxes),
            ", ".join(f"{b.sandbox_id}({b.running_version}->{b.current_version})" for b in stale),
        )
    return {
        "tier": settings.host_tier or None,
        "total": len(boxes),
        "drifted": len(stale),
        "stale_sandbox_ids": [b.sandbox_id for b in stale],
        "boxes": [asdict(b) for b in boxes],
    }
