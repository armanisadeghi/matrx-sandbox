"""User-scoped storage layout — single source of truth for "where does this user's
data live" across both tiers.

The two tiers persist user data differently today:

  - **EC2 tier:** S3 prefix per user (``users/{user_id}/hot/`` and ``users/{user_id}/cold/``).
    Configured via the ``S3_BUCKET`` + ``USER_ID`` env vars passed to the sandbox
    container, which the in-container ``hot-sync.sh`` and ``cold-mount.sh`` scripts
    read at startup/shutdown.

  - **Hosted tier (this server):** named Docker volume per user (``matrx-user-{user_id}``).
    The volume lives at ``/var/lib/docker/volumes/matrx-user-<uuid>/_data`` on the host,
    mounted into the spawned container at ``/home/agent``. Volumes survive container
    destruction; they're deleted only via the explicit ``DELETE /users/{uid}/volume``
    admin endpoint.

Both tiers eventually converge on S3 as the authoritative store (see Phase 1.5 of
``docs/PERSISTENCE_PLAN.md``); the hosted-tier volume is a fast local cache + offline
fallback. This module hides the per-tier mechanics from ``sandbox_manager``.
"""

from __future__ import annotations

import logging
import re
from docker.errors import NotFound
from dataclasses import dataclass

from orchestrator.config import settings

logger = logging.getLogger(__name__)


_USER_ID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.IGNORECASE)
_SANDBOX_ID_RE = re.compile(r"^sbx-[a-f0-9]{12}$")


def user_volume_name(user_id: str) -> str:
    """Stable Docker volume name for a user's hosted-tier home directory.

    Format: ``matrx-user-<uuid>`` — readable in ``docker volume ls``. Refusing to
    return anything for an invalid user_id prevents path-traversal-shaped values
    from making it into the volume name.
    """
    if not _USER_ID_RE.match(user_id or ""):
        raise ValueError(f"user_id must be a UUID, got: {user_id!r}")
    return f"matrx-user-{user_id.lower()}"


def ec2_home_volume_name(sandbox_id: str) -> str:
    """Return the server-owned, per-sandbox EC2 home volume name."""
    if not _SANDBOX_ID_RE.fullmatch(sandbox_id or ""):
        raise ValueError(f"sandbox_id must be a generated sandbox id, got: {sandbox_id!r}")
    return f"matrx-ec2-home-{sandbox_id}"


def ensure_ec2_home_volume(docker_client, sandbox_id: str, user_id: str, organization_id: str) -> str:
    """Create the durable local home for a new non-development EC2 sandbox."""
    name = ec2_home_volume_name(sandbox_id)
    labels = {
        "matrx.owner": "orchestrator", "matrx.sandbox_id": sandbox_id,
        "matrx.user_id": user_id, "matrx.organization_id": organization_id,
        "matrx.kind": "ec2-home", "matrx.tier": "ec2",
    }
    try:
        docker_client.volumes.get(name)
    except NotFound:
        pass
    else:
        raise RuntimeError("new EC2 home name is already occupied; refusing adoption")
    docker_client.volumes.create(
        name=name, driver="local",
        labels=labels,
    )
    logger.info("Created EC2 durable home %s for sandbox %s", name, sandbox_id)
    return name


def validate_ec2_home_volume(docker_client, reference: str, sandbox) -> str:
    """Prove an existing EC2 home belongs exactly to its recorded row."""
    prefix = "matrx-ec2-home-"
    owner_sandbox_id = reference[len(prefix):] if reference.startswith(prefix) else ""
    # A reset/resume successor keeps the original per-sandbox home reference,
    # so its row id need not equal the volume's immutable owner id.
    if not _SANDBOX_ID_RE.fullmatch(owner_sandbox_id):
        raise RuntimeError("EC2 durable home reference is not a server-owned volume name")
    try:
        volume = docker_client.volumes.get(reference)
        if hasattr(volume, "reload"):
            volume.reload()
    except Exception as exc:
        raise RuntimeError("Recorded EC2 durable home is missing; refusing to create an empty replacement") from exc
    attrs = getattr(volume, "attrs", None) or {}
    labels = attrs.get("Labels") or {}
    if (attrs.get("Name") != reference or attrs.get("Driver") != "local"
            or attrs.get("Options") or attrs.get("Scope") not in {None, "local"}):
        raise RuntimeError("Recorded EC2 durable home is not the expected local volume")
    expected_labels = {
        "matrx.owner": "orchestrator", "matrx.sandbox_id": owner_sandbox_id,
        "matrx.user_id": sandbox.user_id, "matrx.organization_id": sandbox.organization_id,
        "matrx.kind": "ec2-home", "matrx.tier": "ec2",
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise RuntimeError("Recorded EC2 durable home labels do not match its sandbox row")
    return reference


@dataclass
class StorageLocation:
    """Where one user's persistent data lives, for a given tier.

    Returned by :func:`resolve_user_storage`. Consumed by ``create_sandbox``
    (volume mount construction) and ``destroy_sandbox`` (volume preservation).
    """
    tier: str  # "ec2" or "hosted"
    # Hosted tier
    volume_name: str | None = None
    # EC2 tier (and optional hosted-tier S3 backup once Phase 1.5 lands)
    s3_bucket: str | None = None
    s3_hot_prefix: str | None = None
    s3_cold_prefix: str | None = None


def resolve_user_storage(user_id: str, tier: str | None) -> StorageLocation:
    """Single function for "where does this user's data live, for this tier."

    Both tiers always set up storage — there is no "ephemeral sandbox" path for
    user data. If we want to spin up a one-shot ephemeral container we do it
    by deleting the volume / wiping the S3 prefix afterward, not by skipping
    persistence at create time. That keeps the contract simple: the user's
    home directory always survives unless we explicitly destroy it.
    """
    # 🚨 Do not restore `or "ec2"`; resolve_host_tier owns the fail-loud
    # persistence-resource boundary.
    effective_tier = settings.resolve_host_tier(tier)

    if effective_tier == "hosted":
        return StorageLocation(
            tier="hosted",
            volume_name=user_volume_name(user_id),
            # Optional async S3 backup — Phase 1.5; off until AWS creds are
            # provisioned on this server.
            s3_bucket=settings.s3_bucket or None,
            s3_hot_prefix=f"users/{user_id}/hot/" if settings.s3_bucket else None,
            s3_cold_prefix=f"users/{user_id}/cold/" if settings.s3_bucket else None,
        )

    # EC2 tier (default): S3 only, no Docker volume — hot-sync.sh and
    # cold-mount.sh do the round-trip.
    return StorageLocation(
        tier="ec2",
        volume_name=None,
        s3_bucket=settings.s3_bucket,
        s3_hot_prefix=f"users/{user_id}/hot/",
        s3_cold_prefix=f"users/{user_id}/cold/",
    )


def ensure_user_volume(docker_client, user_id: str) -> str:
    """Idempotently ensure the per-user Docker volume exists. Returns its name.

    Docker's ``volumes.create`` is idempotent — calling it on an existing volume
    is a no-op. We tag with labels so an admin can find/clean orphan volumes via
    ``docker volume ls --filter label=matrx.kind=user-home``.
    """
    name = user_volume_name(user_id)
    try:
        docker_client.volumes.get(name)
        return name
    except Exception:
        pass  # not found, fall through to create
    docker_client.volumes.create(
        name=name,
        driver="local",
        labels={
            "matrx.user_id": user_id,
            "matrx.kind": "user-home",
            "matrx.tier": "hosted",
        },
    )
    logger.info("Created per-user Docker volume %s for user %s", name, user_id)
    return name
