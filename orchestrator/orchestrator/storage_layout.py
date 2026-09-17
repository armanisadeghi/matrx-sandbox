"""User-scoped storage layout — single source of truth for "where does this user's
data live" across both tiers.

The two tiers persist user data differently today:

  - **EC2 tier:** S3 prefix per user (``users/{user_id}/hot/`` and ``users/{user_id}/cold/``).
    Configured via the ``S3_BUCKET`` + ``USER_ID`` env vars passed to the sandbox
    container, which the in-container ``hot-sync.sh`` and ``cold-mount.sh`` scripts
    read at startup/shutdown.

  - **Hosted tier (this server):** named Docker volume per (user, ORGANIZATION) —
    ``matrx-user-{user_id}-org-{organization_id}``. The volume lives at
    ``/var/lib/docker/volumes/<name>/_data`` on the host, mounted into the spawned
    container at ``/home/agent``. Volumes survive container destruction; they're
    deleted only via the explicit ``DELETE /users/{uid}/volume`` admin endpoint.

    **Why the organization is part of the key (2026-09-17).** ``/home/agent/cloud-files``
    is a MIRROR of the user's AI Dream files, and since the bridge became
    organization-scoped, ``/list`` and ``/changes`` answer for ONE organization. A
    volume keyed by user alone therefore mixed tenants: a file belonging to another
    of that user's organizations stayed on disk, was never listed and never reported
    deleted, and its next edit was refused 409 by the server — with the sandbox
    side unable to explain why (7 users hold files spanning more than one
    organization). The mirror is a TENANT VIEW: one home per (user, organization),
    which is also what the bridge's answers actually describe.

    Volumes created before that change (``matrx-user-<uuid>``, no org suffix) are
    LEFT IN PLACE, untouched and undeleted — "unreferenced means unfinished, never
    deletable". ``docs/OPERATIONS.md`` § Pre-organization per-user volumes says how
    an operator inspects or recovers one.

    **The FIRST organization-keyed home inherits the legacy one (2026-09-18).**
    Leaving the legacy volume in place is right; letting the user's next sandbox
    open an EMPTY ``/home/agent`` is not. Every one of the 213 hosted users has
    their projects, repos, scratch, ``~/.matrx/session.json`` AND the durable
    cloud-sync queue (``/home/agent/.matrx/runtime/cloud-sync-queue.jsonl``) on
    that unmounted legacy volume — from their seat that is a silent loss. So when
    :func:`ensure_user_volume` CREATES a user's FIRST org-keyed home and a legacy
    volume exists, it copies the legacy contents forward once, one-directionally,
    in a short-lived helper container, BEFORE the box starts; the new volume
    carries ``matrx.inherited_from=<legacy name>``. A copy that fails REFUSES the
    creation and removes the half-copied home — a box never starts on one. The
    legacy volume is never read-write mounted, never modified, never deleted.

    A user's SECOND organization's home starts EMPTY on purpose: the home is that
    organization's tenant view of their files, not a second copy of the first
    tenant's drawer. Copying work forward into a later organization is a
    deliberate operator act (``docs/OPERATIONS.md``).

Both tiers eventually converge on S3 as the authoritative store (see Phase 1.5 of
``docs/PERSISTENCE_PLAN.md``); the hosted-tier volume is a fast local cache + offline
fallback. This module hides the per-tier mechanics from ``sandbox_manager``.
"""

from __future__ import annotations

import logging
import re
import socket
from docker.errors import NotFound
from dataclasses import dataclass

from orchestrator.config import settings

logger = logging.getLogger(__name__)


_USER_ID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.IGNORECASE)
_SANDBOX_ID_RE = re.compile(r"^sbx-[a-f0-9]{12}$")


#: Volumes made before the mirror became a tenant view. Never created, never
#: deleted, never adopted — recognised only so tooling can NAME them.
LEGACY_USER_VOLUME_PREFIX = "matrx-user-"


def user_volume_name(user_id: str, organization_id: str) -> str:
    """Stable Docker volume name for one user's home IN ONE ORGANIZATION.

    Format: ``matrx-user-<uuid>-org-<uuid>`` — readable in ``docker volume ls``.
    Both halves are required and both are validated: refusing anything that is
    not a UUID keeps path-traversal-shaped values out of the volume name, and
    refusing a missing organization is the same rule the rest of the platform
    runs on — every write carries an explicit organization, nothing defaults one.
    """
    if not _USER_ID_RE.match(user_id or ""):
        raise ValueError(f"user_id must be a UUID, got: {user_id!r}")
    if not _USER_ID_RE.match(organization_id or ""):
        raise ValueError(
            "organization_id must be a UUID: the hosted home is one tenant's "
            f"view of that user's files, never a cross-organization mixture. Got: {organization_id!r}"
        )
    return f"matrx-user-{user_id.lower()}-org-{organization_id.lower()}"


def is_legacy_user_volume(name: str) -> bool:
    """True for a pre-2026-09-17 per-user volume (no organization in the key)."""
    if not isinstance(name, str) or not name.startswith(LEGACY_USER_VOLUME_PREFIX):
        return False
    return "-org-" not in name[len(LEGACY_USER_VOLUME_PREFIX):]


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


def resolve_user_storage(
    user_id: str, tier: str | None, organization_id: str | None = None
) -> StorageLocation:
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
        if not organization_id:
            raise ValueError(
                "resolve_user_storage needs the organization on the hosted tier: "
                "the home is per (user, organization), and nothing here may pick "
                "one for the caller."
            )
        return StorageLocation(
            tier="hosted",
            volume_name=user_volume_name(user_id, organization_id),
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


class HomeInheritanceError(RuntimeError):
    """The first org-keyed home could not inherit the user's legacy home."""


#: Labels on the short-lived copy container, so an operator can find one.
_INHERITANCE_HELPER_LABELS = {
    "matrx.kind": "hosted-home-inheritance-helper",
    "matrx.owner": "orchestrator",
}

#: Checked, one-directional copy. ``set -eu`` and no pipeline, so a producer's
#: exit status is never masked (same rule as ``hosted_backup._shell_program``).
#: The target is proven empty first: this only ever runs on a volume THIS call
#: created moments ago, and refusing otherwise means a retry can never merge two
#: homes together. ``cp -a`` preserves mode, ownership, times and symlinks.
_INHERIT_PROGRAM = (
    "set -eu\n"
    "test -d /from\n"
    "test -d /to\n"
    '[ -z "$(ls -A /to)" ] || { echo "target home is not empty"; exit 3; }\n'
    "cp -a /from/. /to/\n"
    "sync\n"
    "echo MATRX_HOME_INHERITED=ok\n"
)

_INHERIT_RECEIPT = "MATRX_HOME_INHERITED=ok"


def legacy_user_volume_name(user_id: str) -> str:
    """The pre-organization name for this user's home. Never created here."""
    if not _USER_ID_RE.match(user_id or ""):
        raise ValueError(f"user_id must be a UUID, got: {user_id!r}")
    return f"{LEGACY_USER_VOLUME_PREFIX}{user_id.lower()}"


def _org_home_prefix(user_id: str) -> str:
    return f"{LEGACY_USER_VOLUME_PREFIX}{user_id.lower()}-org-"


def _existing_volume_names(docker_client) -> list[str]:
    volumes = docker_client.volumes.list()
    names = []
    for volume in volumes or []:
        attrs = getattr(volume, "attrs", None) or {}
        name = attrs.get("Name") or getattr(volume, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _legacy_home_to_inherit(docker_client, user_id: str, new_name: str) -> str | None:
    """Return the legacy volume this brand-new home should inherit, or None.

    Two conditions, both required: a legacy per-user volume exists, and this is
    the user's FIRST org-keyed home. A second organization is a different tenant
    view and starts empty on purpose.
    """
    legacy = legacy_user_volume_name(user_id)
    try:
        docker_client.volumes.get(legacy)
    except NotFound:
        return None
    except Exception as exc:  # noqa: BLE001 — an unreadable daemon is not "absent"
        raise HomeInheritanceError(
            f"could not establish whether legacy home {legacy} exists: {exc}"
        ) from exc
    prefix = _org_home_prefix(user_id)
    try:
        existing = _existing_volume_names(docker_client)
    except Exception as exc:  # noqa: BLE001
        raise HomeInheritanceError(
            "could not list volumes to establish whether this is the user's "
            f"first organization home: {exc}"
        ) from exc
    others = [n for n in existing if n.startswith(prefix) and n != new_name]
    if others:
        logger.info(
            "User %s already has %d organization home(s); %s starts empty "
            "(a tenant view, not a second copy of the legacy home)",
            user_id, len(others), new_name,
        )
        return None
    return legacy


def _inheritance_helper_image(docker_client) -> str:
    """The orchestrator's OWN image — a controlled identity, never a pulled tag.

    Same rule as ``hosted_runtime._helper_image`` on the hosted tier: the copy
    runs in the image this process is already running, so nothing new is fetched
    and the helper cannot be swapped underneath us.
    """
    identity = socket.gethostname()
    container = docker_client.containers.get(identity)
    if hasattr(container, "reload"):
        container.reload()
    if not getattr(container, "id", "").startswith(identity):
        raise HomeInheritanceError(
            "cannot establish a controlled orchestrator helper identity for the "
            "home-inheritance copy"
        )
    image = (getattr(container, "attrs", None) or {}).get("Image") or ""
    if not image:
        raise HomeInheritanceError("orchestrator helper image identity is empty")
    return image


def _copy_home_forward(docker_client, legacy_name: str, new_name: str) -> None:
    """Copy the legacy home into the brand-new org-keyed home, once."""
    image = _inheritance_helper_image(docker_client)
    output = docker_client.containers.run(
        image,
        command=[_INHERIT_PROGRAM],
        entrypoint=["/bin/sh", "-ec"],
        volumes={
            # Read-only: the legacy home is never modified by this platform.
            legacy_name: {"bind": "/from", "mode": "ro"},
            new_name: {"bind": "/to", "mode": "rw"},
        },
        network_disabled=True,
        environment={},
        labels=_INHERITANCE_HELPER_LABELS,
        remove=True,
        detach=False,
    )
    if isinstance(output, bytes):
        output = output.decode("utf-8", "surrogateescape")
    if _INHERIT_RECEIPT not in str(output or ""):
        raise HomeInheritanceError(
            f"the copy helper did not confirm it finished ({new_name}); output: "
            f"{str(output or '')[-500:]!r}"
        )


def inherited_from(docker_client, volume_name: str) -> str | None:
    """The legacy home a hosted home was seeded from, if any."""
    try:
        volume = docker_client.volumes.get(volume_name)
        if hasattr(volume, "reload"):
            volume.reload()
    except Exception:  # noqa: BLE001
        return None
    labels = (getattr(volume, "attrs", None) or {}).get("Labels") or {}
    value = labels.get("matrx.inherited_from")
    return value if isinstance(value, str) and value else None


def ensure_user_volume(docker_client, user_id: str, organization_id: str) -> str:
    """Idempotently ensure the (user, organization) Docker volume exists.

    Docker's ``volumes.create`` is idempotent — calling it on an existing volume
    is a no-op. We tag with labels so an admin can find/clean orphan volumes via
    ``docker volume ls --filter label=matrx.kind=user-home`` and can select one
    tenant with ``--filter label=matrx.organization_id=<uuid>``.

    When this call CREATES a user's FIRST organization home and that user has a
    pre-2026-09-17 ``matrx-user-<uid>`` volume, the legacy contents are copied
    forward once before the caller mounts anything, and the new volume carries
    ``matrx.inherited_from``. A failed copy raises :class:`HomeInheritanceError`
    and removes the new volume: a box never starts on a half-copied home, and
    the legacy volume is never touched either way.
    """
    name = user_volume_name(user_id, organization_id)
    try:
        docker_client.volumes.get(name)
        return name
    except Exception:
        pass  # not found, fall through to create

    legacy = _legacy_home_to_inherit(docker_client, user_id, name)
    labels = {
        "matrx.user_id": user_id,
        "matrx.organization_id": organization_id,
        "matrx.kind": "user-home",
        "matrx.tier": "hosted",
    }
    if legacy:
        labels["matrx.inherited_from"] = legacy
    docker_client.volumes.create(name=name, driver="local", labels=labels)
    logger.info(
        "Created per-(user, organization) Docker volume %s for user %s in organization %s",
        name, user_id, organization_id,
    )
    if not legacy:
        return name

    try:
        _copy_home_forward(docker_client, legacy, name)
    except Exception as exc:  # noqa: BLE001
        # Never start a box on a half-copied home, and never touch the legacy
        # volume: remove only what this call just created, then refuse.
        removal_note = "removed"
        try:
            docker_client.volumes.get(name).remove(force=True)
        except Exception as remove_exc:  # noqa: BLE001
            removal_note = f"COULD NOT REMOVE ({remove_exc})"
        logger.error(
            "HOME_INHERITANCE_FAILED volume=%s legacy=%s user=%s organization=%s "
            "new_volume=%s — refusing to start a sandbox on a half-copied home. "
            "The legacy volume was not modified. Cause: %s",
            name, legacy, user_id, organization_id, removal_note, exc,
        )
        raise HomeInheritanceError(
            f"could not copy {legacy} forward into {name}: {exc}. The sandbox is "
            "refused rather than started on an empty or half-copied home; the "
            "legacy volume is untouched."
        ) from exc

    logger.warning(
        "HOME_INHERITED volume=%s legacy=%s user=%s organization=%s — the user's "
        "first organization home was seeded from their pre-organization home "
        "(one direction, once). The legacy volume is unchanged and still on disk.",
        name, legacy, user_id, organization_id,
    )
    return name
