"""Journaled volume-backed image replacement; never owns user row content.

The old container and verified backup survive until the database routing and
the actual target both agree. A failed/uncertain step keeps its original phase
so recovery can resume from evidence rather than treating every error alike.
"""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import re
import socket
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from docker.errors import NotFound

from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError, recovery_action, transition

MIGRATION_STATE_DIR = "/var/lib/matrx-migration"


def migration_state_volume_name(sandbox_id: str) -> str:
    """Return the exact orchestrator-owned restart gate volume for one sandbox."""
    if not re.fullmatch(r"sbx-[0-9a-z]+", sandbox_id):
        raise HostedMigrationStateError("sandbox id cannot name migration state volume")
    return f"matrx-migration-state-{sandbox_id}"


def migration_commit_marker(operation: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", operation):
        raise HostedMigrationStateError("migration operation cannot name commit marker")
    return f"{MIGRATION_STATE_DIR}/{operation}.committed"


def _state_volume_mount(container, sandbox_id: str) -> str | None:
    """Return only the exact private state mount; ambiguous mounts fail closed."""
    attrs = getattr(container, "attrs", None) or {}
    mounts = [mount for mount in (attrs.get("Mounts") or [])
              if mount.get("Destination") == MIGRATION_STATE_DIR]
    if not mounts:
        return None
    expected = migration_state_volume_name(sandbox_id)
    if (len(mounts) != 1 or mounts[0].get("Type") != "volume"
            or mounts[0].get("Name") != expected or not mounts[0].get("RW")):
        raise HostedMigrationStateError("migration state mount identity is ambiguous")
    return expected


async def _ensure_migration_state_volume(client, record) -> bool:
    """Create or verify the sandbox-private restart gate without user storage."""
    name = record["state_volume_name"]
    labels = {
        "matrx.owner": "orchestrator",
        "matrx.kind": "migration-state",
        "matrx.sandbox_id": record["sandbox_id"],
    }
    try:
        volume = await _docker(client.volumes.get, name)
        created = False
    except NotFound:
        volume = await _docker(client.volumes.create, name, driver="local", labels=labels)
        created = True
    await _docker(volume.reload)
    attrs = volume.attrs or {}
    actual_labels = attrs.get("Labels") or {}
    if attrs.get("Driver") != "local" or any(actual_labels.get(k) != v for k, v in labels.items()):
        raise HostedMigrationStateError("migration state volume ownership mismatch")
    return created


async def _validate_existing_migration_state_volume(client, record) -> None:
    """Reject a colliding or substituted stable state volume before image pinning."""
    try:
        volume = await _docker(client.volumes.get, record["state_volume_name"])
    except NotFound:
        return
    await _docker(volume.reload)
    attrs = volume.attrs or {}
    labels = attrs.get("Labels") or {}
    if (attrs.get("Driver") != "local"
            or labels.get("matrx.owner") != "orchestrator"
            or labels.get("matrx.kind") != "migration-state"
            or labels.get("matrx.sandbox_id") != record["sandbox_id"]):
        raise HostedMigrationStateError("migration state volume ownership mismatch")


async def remove_migration_state_volume(client, sandbox_id: str, *, expected_name: str | None = None) -> bool:
    """Remove only an unmounted, exactly labelled private migration-state volume."""
    name = migration_state_volume_name(sandbox_id)
    if expected_name is not None and expected_name != name:
        raise HostedMigrationStateError("migration state cleanup identity mismatch")
    try:
        volume = await _docker(client.volumes.get, name)
    except NotFound:
        return False
    await _docker(volume.reload)
    labels = (volume.attrs or {}).get("Labels") or {}
    if ((volume.attrs or {}).get("Driver") != "local"
            or labels.get("matrx.owner") != "orchestrator"
            or labels.get("matrx.kind") != "migration-state"
            or labels.get("matrx.sandbox_id") != sandbox_id):
        raise HostedMigrationStateError("migration state cleanup ownership mismatch")
    consumers = await _docker(client.containers.list, all=True, filters={"volume": name})
    if consumers:
        raise HostedMigrationStateError("migration state volume still has a container consumer")
    await _docker(volume.remove)
    return True


class HostedMigrationBusyError(HostedMigrationStateError):
    """Expected pre-admission activity refusal, safe to retry unchanged."""


class HostedHomeWriterError(HostedMigrationStateError):
    """Another live container can mutate the migration's persistent home."""


def _migration_failure_status(exc: Exception, *, admitted: bool) -> str:
    """Classify only a proven activity refusal as expected busy control flow."""
    if admitted:
        return "recovery_required"
    if isinstance(exc, HostedMigrationBusyError):
        return "busy_deferred"
    return "failed"


async def _docker(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Do not release a host lock while its Docker action still runs in a
        # worker thread. A process crash is handled by the durable intent.
        try:
            await task
        finally:
            raise


async def _finish(awaitable):
    """Keep the volume lock until a noncancellable helper has really stopped."""
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def replacement_config(old, *, image, environment, operation):
    """Carry Docker's inspected create contract, not a lossy kwargs subset."""
    config = copy.deepcopy(old.attrs["Config"])
    host = copy.deepcopy(old.attrs["HostConfig"])
    networks = old.attrs.get("NetworkSettings", {}).get("Networks", {})
    if len(networks) != 1:
        raise HostedMigrationStateError("migration requires one explicitly preserved network")
    network, endpoint = next(iter(networks.items()))
    if host.get("NetworkMode", "").startswith("container:"):
        raise HostedMigrationStateError("shared network identity cannot be replaced safely")
    ipam = endpoint.get("IPAMConfig") or {}
    if not isinstance(ipam, dict) or set(ipam).difference({"IPv4Address", "IPv6Address"}):
        raise HostedMigrationStateError("unsupported static network identity cannot be replaced safely")
    # Docker reports a generated endpoint MAC here even when the create
    # contract did not request one.  Reusing it while the retained source is
    # still attached is a collision.  A caller-requested Config.MacAddress is
    # different: it is an explicit network identity, and cannot be preserved
    # across the overlapping replacement, so refuse rather than silently
    # changing it.
    if config.get("MacAddress"):
        raise HostedMigrationStateError("explicit container MAC cannot be preserved during replacement")
    if host.get("AutoRemove"):
        raise HostedMigrationStateError("auto-remove runtime cannot retain rollback container")
    # Never inherit a generated container hostname or allocate a second anonymous
    # data volume. Every actual mount must be represented by its exact identity.
    if config.get("Hostname") == old.id[:12]:
        config["Hostname"] = ""
    declared = set((config.get("Volumes") or {}).keys())
    mounted = {m.get("Destination") for m in old.attrs.get("Mounts", [])}
    if not declared.issubset(mounted):
        raise HostedMigrationStateError("unresolved anonymous volume in runtime config")
    for mount in old.attrs.get("Mounts", []):
        if mount.get("Destination") == MIGRATION_STATE_DIR:
            if mount.get("Type") != "volume" or not mount.get("RW"):
                raise HostedMigrationStateError("migration state mount is not a writable Docker volume")
            # This is a container-private anonymous volume. Never clone its
            # commit receipt into the next replacement; _hold_runtime_config
            # declares a fresh empty volume for the new operation.
            continue
        if mount.get("RW") and mount.get("Destination") != "/home/agent":
            raise HostedMigrationStateError("writable mount outside the backed-up home is unsupported")
        if mount.get("Type") == "volume" and not any(
            b.split(":", 2)[:2] == [mount.get("Name"), mount.get("Destination")]
            for b in (host.get("Binds") or [])
        ) and not any(m.get("Source") == mount.get("Name") and m.get("Target") == mount.get("Destination")
                      for m in host.get("Mounts", [])):
            raise HostedMigrationStateError("runtime has an implicit volume that cannot be cloned safely")
    config.update(Image=image, Env=environment, HostConfig=host)
    config["Labels"] = dict(config.get("Labels") or {}, **{"matrx.hosted_migration": operation})
    aliases = [a for a in endpoint.get("Aliases") or [] if a not in {old.id, old.id[:12]}]
    config["NetworkingConfig"] = {"EndpointsConfig": {network: {
        "Aliases": aliases, "DriverOpts": endpoint.get("DriverOpts") or {},
        "Links": endpoint.get("Links") or [],
        # This is only submitted after the paused source's durable disconnect
        # receipt.  It permits a rollback/retry source whose exact dynamic IP
        # Docker now reports as IPAMConfig, without overlapping endpoints.
        **({"IPAMConfig": copy.deepcopy(ipam)} if ipam else {}),
    }}}
    return config


def _hold_runtime_config(runtime, *, state_volume: str, operation: str):
    """Make every pre-CAS replacement inert with a restart-durable private gate."""
    held_env = [item for item in runtime.get("Env", []) if not item.startswith((
        "MATRX_MIGRATION_HOLD=", "MATRX_MIGRATION_COMMIT_MARKER=",
    ))]
    runtime["Env"] = [
        *held_env,
        "MATRX_MIGRATION_HOLD=1",
        f"MATRX_MIGRATION_COMMIT_MARKER={migration_commit_marker(operation)}",
    ]
    runtime.setdefault("Volumes", {})[MIGRATION_STATE_DIR] = {}
    host = runtime.setdefault("HostConfig", {})
    binds = []
    for bind in host.get("Binds") or []:
        parts = bind.split(":", 2)
        if parts[1:2] == [MIGRATION_STATE_DIR]:
            if parts[0] != state_volume:
                raise HostedMigrationStateError("migration state bind identity mismatch")
            continue
        binds.append(bind)
    mounts = []
    for mount in host.get("Mounts") or []:
        if mount.get("Target") == MIGRATION_STATE_DIR:
            if mount.get("Type") != "volume" or mount.get("Source") != state_volume:
                raise HostedMigrationStateError("migration state mount contract mismatch")
            continue
        mounts.append(mount)
    host["Binds"] = [*binds, f"{state_volume}:{MIGRATION_STATE_DIR}:rw"]
    if "Mounts" in host:
        host["Mounts"] = mounts
    return runtime


def source_endpoint_identity(old, client_network=None):
    """Capture the one Docker endpoint we must restore for a paused source.

    Docker assigns endpoint MAC addresses itself.  We retain the observed MAC
    for diagnostics only; it is never fed back into create/connect or treated
    as a reconnect contract.
    """
    networks = old.attrs.get("NetworkSettings", {}).get("Networks", {})
    if len(networks) != 1:
        raise HostedMigrationStateError("migration requires one explicitly preserved network")
    network, endpoint = next(iter(networks.items()))
    ipam = endpoint.get("IPAMConfig") or {}
    if not isinstance(ipam, dict) or set(ipam).difference({"IPv4Address", "IPv6Address"}):
        raise HostedMigrationStateError("Docker returned unsupported source endpoint IPAM")
    result = {
        "network": network,
        "network_id": (getattr(client_network, "id", None)
                       or (getattr(client_network, "attrs", {}) or {}).get("Id") or ""),
        "aliases": list(endpoint.get("Aliases") or []),
        "requested_aliases": [alias for alias in endpoint.get("Aliases") or []
                              if alias not in {old.id, old.id[:12]}],
        "ipv4_address": endpoint.get("IPAddress") or "",
        "ipv6_address": endpoint.get("GlobalIPv6Address") or "",
        "mac_address": endpoint.get("MacAddress") or "",
        "endpoint_ipam_config": copy.deepcopy(ipam),
        "network_ipam_config": copy.deepcopy((getattr(client_network, "attrs", {}) or {}).get("IPAM", {}).get("Config", [])),
    }
    # Docker only permits caller-selected IPs on networks whose subnets were
    # explicitly configured by the user.  A default/auto-IPAM network reports
    # a subnet too, but cannot accept ``ipv4_address`` at connect time.  The
    # endpoint's own IPAMConfig is the only durable evidence that this
    # container was explicitly assigned an address; never infer that from the
    # network-wide auto-allocated subnet.
    result["ipv4_explicit_ipam"] = bool(ipam.get("IPv4Address"))
    result["ipv6_explicit_ipam"] = bool(ipam.get("IPv6Address"))
    for key, docker_prefix in (("ipv4_address", "IPPrefixLen"), ("ipv6_address", "GlobalIPv6PrefixLen")):
        address = result[key]
        if not address:
            continue
        try:
            parsed = ipaddress.ip_interface(address if "/" in address else str(ipaddress.ip_address(address)))
        except ValueError as exc:
            raise HostedMigrationStateError("Docker returned an invalid source endpoint address") from exc
        result[key] = str(parsed.ip)
        prefix = endpoint.get(docker_prefix)
        if not isinstance(prefix, int) or prefix < 0 or prefix > parsed.max_prefixlen:
            prefix = parsed.network.prefixlen
        result[key + "_prefixlen"] = prefix
    if not result["ipv4_address"]:
        raise HostedMigrationStateError("source endpoint has no IPv4 address to restore")
    if not isinstance(result["network_id"], str) or not result["network_id"]:
        raise HostedMigrationStateError("Docker returned no immutable source network identity")
    if ipam.get("IPv4Address") and ipam["IPv4Address"].split("/", 1)[0] != result["ipv4_address"]:
        raise HostedMigrationStateError("Docker endpoint IPAM disagrees with inspected IPv4 address")
    if ipam.get("IPv6Address") and ipam["IPv6Address"].split("/", 1)[0] != result.get("ipv6_address"):
        raise HostedMigrationStateError("Docker endpoint IPAM disagrees with inspected IPv6 address")
    return result


def _endpoint_matches(endpoint, identity):
    """Validate the durable endpoint contract after reconnect.

    Auto-IPAM addresses and Docker-generated MACs are observations, not stable
    identities: Docker may legitimately reassign both on reconnect. Explicit
    endpoint IPAM and requested aliases remain strict.
    """
    if not isinstance(endpoint, dict):
        return False
    ipv4 = endpoint.get("IPAddress", "").split("/", 1)[0]
    try:
        if ipaddress.ip_address(ipv4).version != 4:
            return False
    except ValueError:
        return False
    ipv4_prefix = endpoint.get("IPPrefixLen")
    if not isinstance(ipv4_prefix, int) or not 0 <= ipv4_prefix <= 32:
        return False
    if identity.get("ipv4_explicit_ipam"):
        if ipv4 != identity["ipv4_address"]:
            return False
        if ipv4_prefix != identity.get("ipv4_address_prefixlen"):
            return False
    if identity.get("ipv6_explicit_ipam"):
        if endpoint.get("GlobalIPv6Address", "").split("/", 1)[0] != identity.get("ipv6_address"):
            return False
        if endpoint.get("GlobalIPv6PrefixLen") != identity.get("ipv6_address_prefixlen"):
            return False
    # Docker is allowed to regenerate its endpoint MAC after reconnect.  It is
    # recorded for diagnostics only; explicit caller-requested MACs were
    # already refused at admission, so no consumer contract depends on it.
    return set(endpoint.get("Aliases") or []) == set(identity["aliases"])


async def _disconnect_paused_source(record, old, client):
    """Durably fence the removal of the paused source endpoint."""
    network = await _docker(client.networks.get, record["source_endpoint"]["network"])
    if getattr(network, "id", None) != record["source_endpoint"]["network_id"]:
        raise HostedMigrationStateError("source network name no longer binds the journaled network ID")
    await _docker(network.disconnect, old, force=False)
    await _docker(old.reload)
    if record["source_endpoint"]["network"] in old.attrs.get("NetworkSettings", {}).get("Networks", {}):
        raise HostedMigrationStateError("paused source endpoint remained attached after disconnect")
    return {"old_id": old.id, "network": record["source_endpoint"]["network"],
            "network_id": record["source_endpoint"]["network_id"], "absent": True}


async def _reconnect_paused_source(record, old, client):
    """Reconstruct only the journaled endpoint before resuming the old PID."""
    identity = record.get("source_endpoint")
    receipt = record.get("network_disconnect_receipt")
    if not isinstance(identity, dict) or not isinstance(receipt, dict) or receipt.get("old_id") != old.id:
        raise HostedMigrationStateError("cannot reconnect source without its durable disconnect receipt")
    network = await _docker(client.networks.get, identity["network"])
    if getattr(network, "id", None) != identity["network_id"]:
        raise HostedMigrationStateError("source network name no longer binds the journaled network ID")
    # A process may die after Docker connects the endpoint but before recovery
    # records/resumes it. Treat the already-valid endpoint as the receipt;
    # never issue a duplicate connect that Docker rejects as name-conflicting.
    await _docker(old.reload)
    existing = old.attrs.get("NetworkSettings", {}).get("Networks", {}).get(identity["network"])
    if existing is not None:
        if not _endpoint_matches(existing, identity):
            raise HostedMigrationStateError("existing source endpoint disagrees with reconnect receipt")
        return
    kwargs = _source_reconnect_kwargs(identity)
    await _docker(network.connect, old, **kwargs)
    await _docker(old.reload)
    endpoint = old.attrs.get("NetworkSettings", {}).get("Networks", {}).get(identity["network"])
    if not _endpoint_matches(endpoint, identity):
        raise HostedMigrationStateError("Docker did not restore the exact paused source endpoint")


def _source_reconnect_kwargs(identity):
    """Avoid Docker's forbidden static-IP API on auto-IPAM networks."""
    kwargs = {"aliases": identity.get("requested_aliases") or []}
    if identity.get("ipv4_explicit_ipam"):
        kwargs["ipv4_address"] = identity["ipv4_address"]
    if identity.get("ipv6_explicit_ipam") and identity.get("ipv6_address"):
        kwargs["ipv6_address"] = identity["ipv6_address"]
    return kwargs


async def _resolve_disconnect_intent(record, old, client, journal):
    """Turn an intent-only crash into evidence by inspecting Docker, never guessing."""
    if record.get("phase") != "network_disconnect_intent" or record.get("network_disconnect_receipt"):
        return
    identity = record["source_endpoint"]
    endpoint = old.attrs.get("NetworkSettings", {}).get("Networks", {}).get(identity["network"])
    if endpoint is None:
        receipt = {"old_id": old.id, "network": identity["network"],
                   "network_id": identity["network_id"], "absent": True}
        next_record = transition(record, "network_disconnected", network_disconnect_receipt=receipt)
        record.clear(); record.update(next_record); journal.write(record)
        return
    if not _endpoint_matches(endpoint, identity):
        raise HostedMigrationStateError("source endpoint disagrees with disconnect intent")


def _identity(row):
    return {key: str(getattr(row, key, "")) for key in
            ("sandbox_id", "user_id", "organization_id", "created_at")}


async def _current(record, store):
    row = await store.get(record["sandbox_id"])
    life = await store.get_lifecycle(record["sandbox_id"])
    if (row is None or life is None or life.get("deleted")
            or life.get("status") not in {"running", "ready", "starting"}
            or _identity(row) != record["row_identity"]):
        raise HostedMigrationStateError("sandbox row was deleted or changed; preserve artifacts")
    return row


def _home(container, volume):
    mounts = (container.attrs or {}).get("Mounts", [])
    return any(m.get("Type") == "volume" and m.get("Name") == volume
               and m.get("Destination") == "/home/agent" and m.get("RW") is True for m in mounts)


def _promotion(record):
    return record.get("storage_kind") == "ec2_writable_layer"


def _original_home(container, record):
    if not _promotion(record):
        return _home(container, record["source_volume"])
    return (container.attrs.get("GraphDriver") == record["source_graph_driver"]
            and not any(m.get("Destination") in {"/", "/home", "/home/agent"}
                        or m.get("Destination", "").startswith("/home/agent/")
                        for m in container.attrs.get("Mounts", [])))


def _require_paused_source(old):
    """An exited source cannot be safely booted just to complete rollback."""
    if old.status != "paused":
        raise HostedMigrationStateError("old migration source is not paused; refuse destructive restart")


def _original_process_matches(record, old):
    """Pre-copy recovery resumes only the exact frozen Unix process."""
    expected = record.get("old_process_identity")
    if not isinstance(expected, dict):
        return False
    state = old.attrs.get("State") or {}
    return state.get("Pid") == expected.get("pid") and state.get("StartedAt") == expected.get("started_at")


async def _assert_attached_original_endpoint(record, old, client):
    identity = record["source_endpoint"]
    network = await _docker(client.networks.get, identity["network"])
    if getattr(network, "id", None) != identity["network_id"]:
        raise HostedMigrationStateError("source network name no longer binds the journaled network ID")
    endpoint = old.attrs.get("NetworkSettings", {}).get("Networks", {}).get(identity["network"])
    if not _endpoint_matches(endpoint, identity):
        raise HostedMigrationStateError("original endpoint changed before pre-copy recovery")


async def _remove_verified_pre_copy_helper(client, record):
    """Discard only a partial helper with an RO source and exact reserved backup."""
    if _promotion(record):
        return
    from orchestrator.hosted_backup import _HELPER_LABELS
    containers = await _docker(client.containers.list, all=True, filters={"volume": record["source_volume"]})
    for helper in containers:
        await _docker(helper.reload)
        mounts = helper.attrs.get("Mounts", [])
        source_ro = any(m.get("Type") == "volume" and m.get("Name") == record["source_volume"]
                        and m.get("Destination") == "/source" and not m.get("RW") for m in mounts)
        backup_rw = any(m.get("Type") == "volume" and m.get("Name") == record["backup_name"]
                        and m.get("Destination") == "/backup" and m.get("RW") for m in mounts)
        if not source_ro and not backup_rw:
            continue
        labels = helper.labels or {}
        canonical_labels = all(labels.get(key) == value for key, value in _HELPER_LABELS.items())
        if not (source_ro and backup_rw and helper.attrs.get("Image") == record["helper_image"]
                and canonical_labels):
            raise HostedMigrationStateError("unrecognized container touches pre-copy backup home")
        await _docker(helper.remove, force=True)


async def _recover_pre_copy_source(record, *, store, client, journal):
    """Resume unchanged original state from admitted/old_stopped/backup_intent."""
    old = await _get(client, record["old_id"])
    if (old is None or old.attrs.get("Image") != record["old_image"]
            or not _original_home(old, record) or not _original_process_matches(record, old)):
        raise HostedMigrationStateError("pre-copy original no longer has its recorded process identity")
    if old.status not in {"running", "paused"}:
        raise HostedMigrationStateError("pre-copy original is neither running nor paused")
    await _assert_attached_original_endpoint(record, old, client)
    if await _get(client, record["target_name"]) is not None:
        raise HostedMigrationStateError("target exists during pre-copy recovery")
    if record["phase"] == "backup_intent":
        await _remove_verified_pre_copy_helper(client, record)
    fresh = await _current(record, store)
    if fresh.container_id != old.id:
        raise HostedMigrationStateError("pre-copy row routing changed before original resume")
    await _assert_no_unowned_home_writer(client, record, allowed_ids={old.id})
    if old.status == "paused":
        await _docker(old.unpause)
        await _docker(old.reload)
        if old.status != "running":
            raise HostedMigrationStateError("original did not resume from pre-copy pause")
    return old


def _require_rollback_safe_target(target):
    """Only targets which cannot execute user-home writes may be removed."""
    if target.status == "running":
        raise HostedMigrationStateError("target is running; cannot prove shared home before rollback")
    if target.status not in {"created", "exited", "dead", "paused"}:
        raise HostedMigrationStateError("target state cannot be proven safe for rollback")


async def _pause_target_for_rollback(record, target, journal):
    """Freeze the exact journal-owned target before examining shared home.

    A normal target health/start failure can leave it running.  Pausing is the
    only acceptable quiesce action here: stop/unpause would enter image hooks
    that can write user home.  The intent reaches durable state before Docker.
    """
    await _docker(target.reload)
    if target.status == "running":
        next_record = transition(record, "target_quiesce_intent")
        record.clear(); record.update(next_record); journal.write(record)
        await _docker(target.pause)
        await _docker(target.reload)
        if target.status != "paused":
            raise HostedMigrationStateError("target did not pause for rollback home verification")
    _require_rollback_safe_target(target)


async def _helper_image(client):
    from orchestrator.config import settings
    if settings.host_tier == "ec2":
        # Deployment pins the controlled backup image independently of mutable
        # template tags. A missing receipt is a preflight refusal, not fallback.
        image_id = (Path(__file__).resolve().parent.parent / ".migration-helper-image").read_text().strip()
        image = await _docker(client.images.get, image_id)
        if image.id != image_id:
            raise HostedMigrationStateError("controlled EC2 helper image changed")
        return image_id
    helper = await _get(client, socket.gethostname())
    if helper is None or not helper.id.startswith(socket.gethostname()):
        raise HostedMigrationStateError("cannot establish controlled orchestrator helper identity")
    return helper.attrs.get("Image", "")


async def _pin_helper_image(client, image_id, operation):
    """Keep the exact helper image reachable across a pending journal recovery."""
    pin = f"matrx-migration-helper:{operation}"
    image = await _docker(client.images.get, image_id)
    await _docker(image.tag, "matrx-migration-helper", operation)
    pinned = await _docker(client.images.get, pin)
    if pinned.id != image_id:
        raise HostedMigrationStateError("operation helper pin does not bind the controlled image")
    return pin


async def _get(client, identity):
    try:
        result = await _docker(client.containers.get, identity)
        await _docker(result.reload)
        return result
    except NotFound:
        return None


async def _target(record, client):
    target = await _get(client, record.get("target_id") or record["target_name"])
    if target is None:
        return None
    if (record.get("target_id") and target.id != record["target_id"]
            or (target.labels or {}).get("matrx.hosted_migration") != record["operation_label"]
            or (target.attrs or {}).get("Image") != record["target_image"]
            or not _home(target, record["source_volume"])):
        raise HostedMigrationStateError("target identity does not match durable intent")
    return target


async def _assert_no_unowned_home_writer(client, record, *, allowed_ids):
    """Refuse destructive recovery while another container can mutate this home."""
    containers = await _docker(
        client.containers.list,
        all=True,
        filters={"volume": record["source_volume"]},
    )
    for container in containers:
        await _docker(container.reload)
        if container.id in allowed_ids:
            continue
        writes_volume = any(
            mount.get('Type') == 'volume' and mount.get('Name') == record['source_volume'] and mount.get('RW')
            for mount in container.attrs.get('Mounts', [])
        )
        if container.status not in {"created", "exited", "dead"} and writes_volume:
            raise HostedHomeWriterError(
                "another running container can write this home during recovery"
            )


async def _assert_migration_home_exclusive(client, record, *, allowed_ids):
    """Translate a safe pre-admission sibling refusal into actionable control flow."""
    try:
        await _assert_no_unowned_home_writer(client, record, allowed_ids=allowed_ids)
    except HostedHomeWriterError as exc:
        raise HostedMigrationBusyError(
            "another running sandbox shares this persistent home; stop that "
            "sandbox, then retry the image update; no container or file was changed"
        ) from exc


async def _quiesce_target(target, record):
    """Force-remove a target without invoking its shutdown hooks on shared home."""
    await _docker(target.reload)
    # ``force=True`` is SIGKILL/removal, not an image entrypoint shutdown.  A
    # graceful stop would itself be a user-home write for these images.
    await _docker(target.remove, force=True)


async def _ensure_rollback_home(record, client, journal):
    """Return a verified rollback receipt, restoring only proven target drift.

    A held replacement is not trusted to leave the shared home untouched. If
    it changed that home, rollback must use the already-verified backup rather
    than strand both containers paused forever. Corrupt/missing backup evidence
    still fails closed before the restore helper can mount the home writable.
    """
    from orchestrator.hosted_backup import (
        HostedBackupError,
        restore_volume,
        verify_volume_unchanged,
    )

    try:
        receipt = await _finish(verify_volume_unchanged(
            client,
            receipt=record["backup_receipt"],
            image=record["helper_image"],
        ))
    except HostedBackupError as exc:
        if str(exc) != "shared home manifest changed while migration target was held":
            raise
        restored = transition(record, "restore_intent")
        record.clear(); record.update(restored); journal.write(record)
        await _finish(restore_volume(
            client,
            receipt=record["backup_receipt"],
            image=record["helper_image"],
        ))
        receipt = await _finish(verify_volume_unchanged(
            client,
            receipt=record["backup_receipt"],
            image=record["helper_image"],
        ))
    record["rollback_home_receipt"] = receipt
    journal.write(record)
    return receipt


async def _ready(container, record, *, target):
    from orchestrator.migrate import _wait_container_ready, _container_version
    await _docker(container.reload)
    expected = record["target_image"] if target else record["old_image"]
    if (container.status != "running" or container.name.lstrip("/") != record["sandbox_id"]
            or container.attrs.get("Image") != expected
            or not (_home(container, record["source_volume"]) if target else _original_home(container, record))):
        return False
    if not await _wait_container_ready(container, record["verify_timeout"], record["template"]):
        return False
    return not target or not record["target_version"] or await _container_version(container) == record["target_version"]


async def _migration_state(container):
    """Read the image-owned migration state without treating HTTP 200 as active."""
    result = await _docker(container.exec_run, ["/bin/sh", "-ec", "curl -fsS --max-time 3 http://127.0.0.1:8000/health"])
    code = getattr(result, "exit_code", result[0] if isinstance(result, tuple) else None)
    output = getattr(result, "output", result[1] if isinstance(result, tuple) and len(result) > 1 else b"")
    if code != 0:
        raise HostedMigrationStateError("target health endpoint did not answer for migration attestation")
    try:
        payload = json.loads(output.decode() if isinstance(output, bytes) else str(output))
    except (TypeError, ValueError) as exc:
        raise HostedMigrationStateError("target health endpoint has no migration state attestation") from exc
    state = payload.get("migration_state") if isinstance(payload, dict) else None
    if state not in {"held", "active"}:
        raise HostedMigrationStateError("target health endpoint has invalid migration state attestation")
    return state


async def _wait_migration_state(container, expected, timeout):
    """Poll the image-owned state until activation completes or the real deadline expires."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if await _migration_state(container) == expected:
                return
        except HostedMigrationStateError as exc:
            last_error = exc
        await asyncio.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
    detail = f": {last_error}" if last_error else ""
    raise HostedMigrationStateError(f"EC2 target did not attest {expected} before activation deadline{detail}")


_ACTIVATION_HOME_PREFLIGHT = r'''
set -eu
reject_symlink_chain() {
  path="$1"; current=""
  old_ifs="$IFS"; IFS=/
  for part in ${path#/}; do
    [ -n "$part" ] || continue
    current="$current/$part"
    [ ! -L "$current" ] || {
      echo "required lifecycle path contains a symlink: $current" >&2; IFS="$old_ifs"; exit 45;
    }
  done
  IFS="$old_ifs"
}
can_create_under() {
  parent="$1"
  while [ ! -e "$parent" ]; do parent="${parent%/*}"; [ -n "$parent" ] || parent=/; done
  [ -d "$parent" ] && [ -w "$parent" ] && [ -x "$parent" ]
}
require_dir_writer() {
  path="$1"
  reject_symlink_chain "$path"
  if [ -e "$path" ]; then
    [ -d "$path" ] && [ -w "$path" ] && [ -x "$path" ] || {
      echo "required runtime directory is not agent-writable: $path" >&2; exit 41;
    }
  else
    can_create_under "${path%/*}" || {
      echo "required runtime directory cannot be created by agent: $path" >&2; exit 42;
    }
  fi
}
require_file_writer() {
  path="$1"
  reject_symlink_chain "$path"
  if [ -e "$path" ]; then
    [ -f "$path" ] && [ -w "$path" ] || {
      echo "required lifecycle file is not agent-writable: $path" >&2; exit 43;
    }
  else
    can_create_under "${path%/*}" || {
      echo "required lifecycle file cannot be created by agent: $path" >&2; exit 44;
    }
  fi
}
[ -d /home/agent ] && [ -x /home/agent ] || {
  echo "mounted agent home is unavailable" >&2; exit 40;
}
require_dir_writer /home/agent/.matrx
require_file_writer /home/agent/.matrx/session-report.md
require_dir_writer /home/agent/.matrx/locks
require_dir_writer /home/agent/.matrx/runtime
if [ -n "${MATRX_AIDREAM_URL:-}" ] && [ -n "${MATRX_AIDREAM_SERVICE_TOKEN:-}" ] \
   && [ -n "${USER_ID:-}" ] && [ -n "${ORGANIZATION_ID:-}" ]; then
  require_dir_writer /home/agent/cloud-files
fi
'''


async def _preflight_activation_home(target) -> dict:
    """Prove required agent-owned lifecycle paths before irreversible CAS."""
    result = await _docker(
        target.exec_run, ["/bin/sh", "-ec", _ACTIVATION_HOME_PREFLIGHT], user="agent",
    )
    code = getattr(result, "exit_code", result[0] if isinstance(result, tuple) else None)
    output = getattr(result, "output", result[1] if isinstance(result, tuple) and len(result) > 1 else b"")
    if code != 0:
        reason = output.decode(errors="replace") if isinstance(output, bytes) else str(output or "")
        reason = " ".join(reason.strip().split())[:500] or "agent write preflight failed"
        raise HostedMigrationStateError(
            f"replacement cannot activate required persistence safely: {reason}; "
            "the original sandbox is unchanged"
        )
    return {"target_id": target.id, "agent_lifecycle_paths_writable": True}


async def _activate_promoted_target(record, *, target, store, client, journal):
    """Release a post-CAS held target only after its durable pre-CAS proof."""
    if _promotion(record):
        if not isinstance(record.get("postboot_verified_receipt"), dict):
            raise HostedMigrationStateError("cannot activate EC2 target without durable postboot verification")
    elif not isinstance(record.get("pre_cas_home_receipt"), dict):
        raise HostedMigrationStateError("cannot activate hosted target without held-home verification")
    preflight = record.get("activation_home_preflight_receipt")
    if (not isinstance(preflight, dict) or preflight.get("target_id") != target.id
            or preflight.get("agent_lifecycle_paths_writable") is not True):
        raise HostedMigrationStateError("cannot activate target without durable lifecycle-path preflight")
    current = await _current(record, store)
    if current.container_id != target.id:
        raise HostedMigrationStateError("routing changed before target activation")
    expected_row_home = record["source_volume"] if _promotion(record) else record.get("row_persistence_volume")
    if current.persistence_volume != expected_row_home or not _home(target, record["source_volume"]):
        raise HostedMigrationStateError("committed home routing does not match target")
    if record["phase"] != "activation_intent":
        next_record = transition(record, "activation_intent")
        record.clear()
        record.update(next_record)
        journal.write(record)
    await _docker(target.reload)
    if target.status == "paused":
        await _docker(target.unpause)
    elif target.status != "running":
        raise HostedMigrationStateError("target is not held/running for activation")
    await _docker(target.reload)
    if target.status != "running":
        raise HostedMigrationStateError("target did not resume for activation")
    activated = await _docker(target.exec_run, [
        "/bin/sh", "-ec",
        f"install -d -m 0711 {MIGRATION_STATE_DIR} && "
        f"touch {migration_commit_marker(record['operation_label'])} && "
        f"chmod 0444 {migration_commit_marker(record['operation_label'])}",
    ])
    code = getattr(activated, "exit_code", activated[0] if isinstance(activated, tuple) else None)
    if code != 0:
        raise HostedMigrationStateError("target activation marker could not be written")
    if not await _ready(target, record, target=True):
        raise HostedMigrationStateError("target did not become ready after activation")
    await _wait_migration_state(target, "active", record["verify_timeout"])
    next_record = transition(record, "committed", activation_receipt={
        "target_id": target.id, "migration_state": "active",
    })
    record.clear()
    record.update(next_record)
    journal.write(record)
    await _cleanup(record, client, journal, committed=True)
    return {"status": "committed", "sandbox_id": record["sandbox_id"]}


def _record_error(record, journal, exc):
    # Preserve the phase, including commit_intent after an ambiguous DB call.
    record["last_error"] = type(exc).__name__ + ": " + str(exc)
    journal.write(record)
    return {"status": "recovery_required", "sandbox_id": record["sandbox_id"],
            "phase": record["phase"], "reason": record["last_error"]}


async def _cleanup(record, client, journal, *, committed):
    receipts = record.setdefault("cleanup_receipt", {})
    obsolete_id = record["old_id"] if committed else record.get("target_id")
    if obsolete_id:
        obsolete = await _get(client, obsolete_id)
        if obsolete is not None:
            # The old source remains paused through CAS.  Force-removing the
            # exact recorded ID avoids an entrypoint shutdown which can chown
            # or otherwise mutate its home layer.
            await _docker(obsolete.remove, force=True)
        receipts["obsolete_container_removed"] = obsolete_id
        journal.write(record)
    backup_name = record.get("backup_name")
    if backup_name:
        try:
            backup = await _docker(client.volumes.get, backup_name)
        except NotFound:
            backup = None
        if backup is not None:
            if (backup.attrs.get("Labels") or {}).get("matrx.hosted_migration") != record["operation_label"]:
                raise HostedMigrationStateError("backup cleanup ownership mismatch")
            await _docker(backup.remove)
        receipts["backup_volume_removed"] = backup_name
        journal.write(record)
    helper_pin = record.get("helper_image_pin")
    if helper_pin:
        intent = receipts.get("helper_image_pin_removal_intent")
        expected_intent = {"pin": helper_pin, "image": record["helper_image"]}
        if intent is None:
            try:
                pinned = await _docker(client.images.get, helper_pin)
            except NotFound as exc:
                creation_intent = record.get("helper_image_pin_creation_intent")
                if (creation_intent != expected_intent
                        or isinstance(record.get("helper_image_pin_created"), dict)):
                    raise HostedMigrationStateError("helper pin is absent before durable removal intent") from exc
                receipts["helper_image_pin_never_created"] = helper_pin
                helper_pin = None
                journal.write(record)
            if helper_pin:
                if pinned.id != record["helper_image"]:
                    raise HostedMigrationStateError("helper pin no longer binds the journal helper image")
                receipts["helper_image_pin_removal_intent"] = expected_intent
                journal.write(record)
        elif intent != expected_intent:
            raise HostedMigrationStateError("helper pin removal intent does not bind the journal helper image")
        if helper_pin:
            try:
                pinned = await _docker(client.images.get, helper_pin)
            except NotFound:
                # A process can die after Docker removes the tag but before it
                # writes the receipt.  The earlier durable intent is the fence.
                if receipts.get("helper_image_pin_removal_intent") != expected_intent:
                    raise HostedMigrationStateError("helper pin is absent without durable removal intent")
                receipts["helper_image_pin_removed"] = helper_pin
            else:
                if pinned.id != record["helper_image"]:
                    raise HostedMigrationStateError("helper pin no longer binds the journal helper image")
                tags = set(getattr(pinned, "tags", None) or (pinned.attrs or {}).get("RepoTags") or ())
                if helper_pin not in tags:
                    raise HostedMigrationStateError("helper pin is not present in its image tag inventory")
                references = []
                if tags == {helper_pin}:
                    references = await _docker(
                        client.containers.list, all=True, filters={"ancestor": pinned.id},
                    )
                if references:
                    receipts["helper_image_pin_retained"] = {
                        "pin": helper_pin, "image": pinned.id, "reason": "last_tag_in_use",
                    }
                else:
                    await _docker(client.images.remove, helper_pin, noprune=True, force=False)
                    receipts["helper_image_pin_removed"] = helper_pin
        journal.write(record)
    if _promotion(record) and not committed:
        try:
            volume = await _docker(client.volumes.get, record["source_volume"])
        except NotFound:
            volume = None
        if volume is not None:
            if (volume.attrs.get("Labels") or {}).get("matrx.ec2_home_copy") != record["operation_label"]:
                raise HostedMigrationStateError("promoted home cleanup ownership mismatch")
            await _docker(volume.remove)
        receipts["promoted_home_removed"] = record["source_volume"]
    state_intent = record.get("state_volume_creation_intent")
    if (not committed and isinstance(state_intent, dict)
            and state_intent.get("name") == record.get("state_volume_name")
            and not record.get("old_state_volume")):
        await remove_migration_state_volume(
            client, record["sandbox_id"], expected_name=record["state_volume_name"],
        )
        receipts["migration_state_volume_removed"] = record["state_volume_name"]
    record["cleanup_complete"] = True
    record.pop("last_error", None)
    journal.write(record)


async def recover_hosted_migration(record, *, store, client, journal, locked=False, copy_locked=False):
    """Recover only exact journal identities; DB ambiguity never authorizes restore."""
    if not locked:
        with ExitStack() as locks:
            locks.enter_context(journal.lock(record["sandbox_id"]))
            for key in sorted({record["source_volume"], record.get("source_home_key", record["source_volume"])}):
                locks.enter_context(journal.lock("volume-" + key))
            fresh = journal.read(record["sandbox_id"])
            if fresh is None:
                raise HostedMigrationStateError("recovery journal disappeared")
            return await recover_hosted_migration(fresh, store=store, client=client, journal=journal, locked=True)
    if _promotion(record) and not copy_locked:
        try:
            with journal.lock("copy-" + record["operation_label"]):
                return await recover_hosted_migration(record, store=store, client=client,
                                                     journal=journal, locked=True, copy_locked=True)
        except Exception as exc:
            return _record_error(record, journal, exc)
    try:
        current = await _current(record, store)
        try:
            source = await _docker(client.volumes.get, record["source_volume"])
        except NotFound:
            if not _promotion(record) or record["phase"] not in {"admitted", "old_stopped", "backup_intent", "recovered"}:
                raise
            source = None
        from orchestrator.hosted_backup import _volume_identity
        if source is not None and (not _promotion(record) or record.get("backup_receipt")) and _volume_identity(source) != record["source_identity"]:
            raise HostedMigrationStateError("source volume identity changed")
        if recovery_action(record, db_container_id=current.container_id, target_exists_ready=False) == "resume_pre_copy_source":
            old = await _recover_pre_copy_source(record, store=store, client=client, journal=journal)
            if not await _ready(old, record, target=False):
                raise HostedMigrationStateError("pre-copy original did not become ready after resume")
            record["phase"] = "recovered"
            journal.write(record)
            await _cleanup(record, client, journal, committed=False)
            return {"status": "recovered", "sandbox_id": record["sandbox_id"]}
        if record["phase"] == "recovered":
            old = await _get(client, record["old_id"])
            if current.container_id != record["old_id"] or old is None or not await _ready(old, record, target=False):
                raise HostedMigrationStateError("recovered runtime no longer matches its cleanup receipt")
            await _assert_no_unowned_home_writer(client, record, allowed_ids={old.id})
            await _cleanup(record, client, journal, committed=False)
            return {"status": "recovered", "sandbox_id": record["sandbox_id"]}
        target = await _target(record, client)
        if target is not None and not record.get("target_id"):
            record["target_id"] = target.id
            journal.write(record)
        await _assert_no_unowned_home_writer(
            client,
            record,
            allowed_ids={record["old_id"], *( [target.id] if target is not None else [])},
        )
        # A successful CAS may have landed just before a process crash.  The
        # EC2 target is deliberately paused at that boundary; it is not ready
        # for users until its durable postboot proof permits activation.
        expected_row_home = record["source_volume"] if _promotion(record) else record.get("row_persistence_volume")
        if (current.container_id == record.get("target_id")
                and current.persistence_volume != expected_row_home):
            raise HostedMigrationStateError("committed home routing does not match target")
        if (target is not None
                and current.container_id == target.id
                and record["phase"] in {"commit_intent", "activation_intent"}):
            return await _activate_promoted_target(record, target=target, store=store,
                                                    client=client, journal=journal)
        target_ok = target is not None and await _ready(target, record, target=True)
        action = recovery_action(record, db_container_id=current.container_id, target_exists_ready=target_ok)
        if action == "finalize_committed":
            record["phase"] = "committed"
            journal.write(record)
            await _cleanup(record, client, journal, committed=True)
            return {"status": "committed", "sandbox_id": record["sandbox_id"]}
        if action != "resume_old" or current.container_id != record["old_id"]:
            raise HostedMigrationStateError("database routing is ambiguous; preserve artifacts")
        old = await _get(client, record["old_id"])
        if old is None or old.attrs.get("Image") != record["old_image"] or not _original_home(old, record):
            raise HostedMigrationStateError("original container identity changed")
        _require_paused_source(old)
        await _resolve_disconnect_intent(record, old, client, journal)
        if target is not None:
            await _pause_target_for_rollback(record, target, journal)
        # The held target may have disappeared after mutating the shared home.
        # Verify (and, for the one proven drift failure, restore) independently
        # of target existence, before the original process can resume.
        if not _promotion(record):
            await _ensure_rollback_home(record, client, journal)
        if target is not None:
            await _quiesce_target(target, record)
        # A concurrent row deletion/change must never cause resurrection.
        if (await _current(record, store)).container_id != record["old_id"]:
            raise HostedMigrationStateError("routing changed before rollback")
        if record.get("network_disconnect_receipt"):
            await _reconnect_paused_source(record, old, client)
        if old.name.lstrip("/") != record["sandbox_id"]:
            await _docker(old.rename, record["sandbox_id"])
        if (await _current(record, store)).container_id != old.id:
            raise HostedMigrationStateError("routing changed before original resume")
        # Both hosted and EC2 entrypoints mutate `/home/agent` on start.  A
        # migration rollback may resume only the retained paused process; an
        # exited old source is evidence we cannot safely replay its boot.
        _require_paused_source(old)
        await _docker(old.unpause)
        if not await _ready(old, record, target=False):
            raise HostedMigrationStateError("original runtime did not recover")
        record["phase"] = "recovered"
        journal.write(record)
        await _cleanup(record, client, journal, committed=False)
        return {"status": "recovered", "sandbox_id": record["sandbox_id"]}
    except Exception as exc:
        return _record_error(record, journal, exc)


async def recover_hosted_migrations(*, store):
    from orchestrator.config import settings
    from orchestrator.sandbox_manager import _get_docker_client
    result = {"recovered": [], "failed": []}
    if settings.host_tier not in {"hosted", "ec2"}:
        return result
    journal = HostedMigrationJournal()
    journal.ensure_ready()
    client = _get_docker_client()
    for record in journal.records():
        if record.get("cleanup_complete"):
            continue
        outcome = await recover_hosted_migration(record, store=store, client=client, journal=journal)
        result["failed" if outcome["status"] == "recovery_required" else "recovered"].append(record["sandbox_id"])
    return result


async def migrate_hosted(sandbox_id, *, old, target, env, volumes, labels, host,
                         cur, store, verify_timeout, platform_env_changes,
                         interrupt_attached_sessions=False):
    """Stop, snapshot, replace, prove routing, then retire exact old artifacts."""
    from orchestrator import activity
    from orchestrator.config import settings
    from orchestrator.hosted_backup import snapshot_volume, _volume_identity
    from orchestrator.knobs import knob_int
    from orchestrator.migrate import _wait_container_ready, _container_version
    from orchestrator.sandbox_manager import _get_docker_client
    from orchestrator.versioning import _version_from_image_attrs
    if settings.host_tier not in {"hosted", "ec2"}:
        return {"status": "unsupported_storage", "sandbox_id": sandbox_id,
                "reason": "this migration contract is for hosted named-volume homes only"}
    journal, client = HostedMigrationJournal(), _get_docker_client()
    record = None
    try:
        await _docker(old.reload)
        homes = [m for m in old.attrs.get("Mounts", []) if m.get("Destination") == "/home/agent"]
        promotion = settings.host_tier == "ec2" and not homes and labels.get("matrx.template") in {"slim", "bare"}
        if not promotion and (len(homes) != 1 or homes[0].get("Type") != "volume" or not homes[0].get("RW")):
            raise HostedMigrationStateError("home must be one writable named Docker volume")
        operation = uuid.uuid4().hex
        from orchestrator.storage_layout import ec2_home_volume_name
        volume = ec2_home_volume_name(sandbox_id) if promotion else homes[0]["Name"]
        source_key = "layer-" + sandbox_id if promotion else volume
        with ExitStack() as locks:
            locks.enter_context(journal.lock(sandbox_id))
            for key in sorted({source_key, volume}):
                locks.enter_context(journal.lock("volume-" + key))
            if any((r["sandbox_id"] == sandbox_id or r["source_volume"] == volume)
                   and not r.get("cleanup_complete") for r in journal.records()):
                raise HostedMigrationStateError("an earlier migration still requires recovery")
            if activity.inflight_count(sandbox_id) or (
                activity.open_session_count(sandbox_id)
                and not interrupt_attached_sessions
            ):
                raise HostedMigrationBusyError(
                    "sandbox still has active work or an interactive session"
                )
            await _docker(old.reload)
            row = await store.get(sandbox_id)
            if (row is None or row.container_id != old.id or old.status != "running"
                    or (not promotion and not _home(old, volume))
                    or (promotion and row.persistence_volume)):
                raise HostedMigrationStateError("original runtime no longer matches routing")
            await _assert_migration_home_exclusive(
                client,
                {"source_volume": volume},
                allowed_ids={old.id},
            )
            image = await _docker(client.images.get, target)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image.id):
                raise HostedMigrationStateError("target image identity is not immutable")
            helper_image = await _helper_image(client)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image):
                raise HostedMigrationStateError("helper image identity is not immutable")
            # Verify dependencies before stopping a user runtime. No user mounts,
            # credentials or network are exposed to this preflight helper.
            await _docker(client.containers.run, helper_image,
                command=["command -v tar getfacl sha256sum sync python3 >/dev/null"],
                entrypoint=["/bin/sh", "-ec"], network_disabled=True,
                environment={}, remove=True)
            runtime = replacement_config(old, image=image.id, environment=env, operation=operation)
            source_network = await _docker(client.networks.get, next(iter(
                old.attrs.get("NetworkSettings", {}).get("Networks", {})
            ), ""))
            source_endpoint = source_endpoint_identity(old, source_network)
            if promotion:
                from orchestrator.ec2_home_copy_client import preflight_helper
                await _finish(preflight_helper())
                runtime["HostConfig"]["Binds"] = [*(runtime["HostConfig"].get("Binds") or []),
                                                   volume + ":/home/agent:rw"]
                source_identity = {"name": volume}
            else:
                source = await _docker(client.volumes.get, volume)
                source_identity = _volume_identity(source)
            state_volume_name = migration_state_volume_name(sandbox_id)
            old_state_volume = _state_volume_mount(old, sandbox_id)
            _hold_runtime_config(runtime, state_volume=state_volume_name, operation=operation)
            stop_timeout = await knob_int("shutdown_timeout_seconds")
            await _validate_existing_migration_state_volume(client, {
                "sandbox_id": sandbox_id, "state_volume_name": state_volume_name,
            })
            helper_image_pin = f"matrx-migration-helper:{operation}"
            record = {
                "schema_version": 2, "sandbox_id": sandbox_id, "row_identity": _identity(row),
                "old_id": old.id, "old_image": old.attrs["Image"], "old_name": old.name.lstrip("/"),
                "old_process_identity": {"pid": old.attrs["State"]["Pid"], "started_at": old.attrs["State"]["StartedAt"]},
                "source_volume": volume, "source_identity": source_identity,
                "target_name": f"{sandbox_id}-mig-{operation}", "target_image": image.id,
                "target_version": _version_from_image_attrs(image.attrs), "operation_label": operation,
                "backup_name": f"matrx-migration-backup-{operation}", "helper_image": helper_image,
                "helper_image_pin": helper_image_pin,
                "helper_image_pin_creation_intent": {
                    "pin": helper_image_pin, "image": helper_image,
                },
                "rollback_name": f"{sandbox_id}-old-{operation}", "template": labels.get("matrx.template"),
                "verify_timeout": verify_timeout, "stop_timeout": stop_timeout,
                "source_endpoint": source_endpoint, "row_persistence_volume": row.persistence_volume,
                "state_volume_name": state_volume_name,
                "old_state_volume": old_state_volume,
                "state_volume_creation_intent": {
                    "name": state_volume_name, "sandbox_id": sandbox_id,
                },
                "phase": "admitted",
            }
            if promotion:
                record.update(storage_kind="ec2_writable_layer", source_home_key=source_key,
                              source_graph_driver=copy.deepcopy(old.attrs.get("GraphDriver")),
                              old_persistence_volume=row.persistence_volume)
                if not _original_home(old, record):
                    raise HostedMigrationStateError("unsupported writable-layer home topology")
            await _current(record, store)
            if promotion:
                # The service creates this inode, so its recovery can acquire
                # it after the root helper exits. The helper never owns it.
                with journal.lock("copy-" + operation):
                    pass
            journal.write(record)
            try:
                record["state_volume_created"] = await _ensure_migration_state_volume(client, record)
                journal.write(record)
                pinned = await _pin_helper_image(client, helper_image, operation)
                if pinned != helper_image_pin:
                    raise HostedMigrationStateError("helper image pin name differs from durable intent")
                record["helper_image_pin_created"] = {
                    "pin": pinned, "image": helper_image,
                }
                journal.write(record)
                await _docker(old.pause)
                await _docker(old.reload)
                if old.status != "paused":
                    raise HostedMigrationStateError("original writer did not stop")
                record = transition(record, "old_stopped"); journal.write(record)
                record = transition(record, "backup_intent"); journal.write(record)
                if await _get(client, record["target_name"]) is not None:
                    raise HostedMigrationStateError("reserved target name is already occupied")
                reserved = volume if promotion else record["backup_name"]
                try:
                    await _docker(client.volumes.get, reserved)
                except NotFound:
                    pass
                else:
                    raise HostedMigrationStateError("reserved storage name is already occupied")
                storage_labels = {"matrx.owner": "orchestrator", "matrx.hosted_migration": operation}
                if promotion:
                    storage_labels.update({"matrx.ec2_home_copy": operation, "matrx.kind": "ec2-home",
                                           "matrx.tier": "ec2",
                                           "matrx.user_id": str(row.user_id),
                                           "matrx.organization_id": str(row.organization_id),
                                           "matrx.sandbox_id": sandbox_id})
                captured = await _docker(client.volumes.create, reserved, driver="local", labels=storage_labels)
                if promotion:
                    record["source_identity"] = _volume_identity(captured)
                    record["copy_operation"] = {
                        "schema_version": 1, "operation": operation, "source_container_id": old.id,
                        "source_image": old.attrs["Image"], "source_pid": old.attrs["State"]["Pid"],
                        "source_started_at": old.attrs["State"]["StartedAt"],
                        "source_overlay_identity": record["source_graph_driver"],
                        "target_volume": volume, "target_created_at": captured.attrs.get("CreatedAt"),
                    }
                    journal.write(record)
                    from orchestrator.ec2_home_copy_client import copy_home
                    receipt = await _finish(copy_home(record["copy_operation"]))
                else:
                    receipt = await _finish(snapshot_volume(client, volume=volume,
                        backup_volume=record["backup_name"], image=helper_image))
                record = transition(record, "backup_verified", backup_receipt=receipt); journal.write(record)
                # Keep the original PID paused.  Docker permits a paused source
                # to be detached, allowing the target to claim its aliases
                # without booting/re-chowning the old image on rollback.
                record = transition(record, "network_disconnect_intent"); journal.write(record)
                disconnected = await _disconnect_paused_source(record, old, client)
                record = transition(record, "network_disconnected", network_disconnect_receipt=disconnected)
                journal.write(record)
                record = transition(record, "target_create_intent"); journal.write(record)
                created = await _docker(client.api.create_container_from_config, runtime, name=record["target_name"])
                record = transition(record, "target_created", target_id=created["Id"]); journal.write(record)
                new = await _target(record, client)
                if new is None:
                    raise HostedMigrationStateError("created target cannot be inspected")
                record = transition(record, "target_start_intent"); journal.write(record)
                await _docker(new.start)
                if not await _wait_container_ready(new, verify_timeout, record["template"]):
                    raise HostedMigrationStateError("target readiness failed")
                version = await _container_version(new)
                if record["target_version"] and version != record["target_version"]:
                    raise HostedMigrationStateError("target baked version differs from requested image")
                record = transition(record, "target_ready"); journal.write(record)
                # `/tmp/.sandbox_ready` proves entrypoint launch, not that the
                # image is holding user work.  Both storage classes must attest
                # held before the target is paused and CAS becomes possible.
                await _wait_migration_state(new, "held", verify_timeout)
                record["activation_home_preflight_receipt"] = await _preflight_activation_home(new)
                journal.write(record)
                record = transition(record, "target_quiesce_intent"); journal.write(record)
                await _docker(new.pause)
                await _docker(new.reload)
                if new.status != "paused":
                    raise HostedMigrationStateError("target did not pause for pre-CAS verification")
                if promotion:
                    from orchestrator.ec2_home_copy_client import verify_home
                    verified = await _finish(verify_home(record["copy_operation"], record["backup_receipt"],
                                                         target_id=new.id, target_image=record["target_image"]))
                    record = transition(record, "postboot_verified", postboot_verified_receipt=verified)
                    journal.write(record)
                else:
                    from orchestrator.hosted_backup import verify_volume_unchanged
                    manifest_receipt = await _finish(verify_volume_unchanged(
                        client, receipt=record["backup_receipt"], image=record["helper_image"]
                    ))
                    record["pre_cas_home_receipt"] = manifest_receipt
                    journal.write(record)
                record = transition(record, "rename_intent"); journal.write(record)
                await _docker(old.rename, record["rollback_name"])
                await _docker(new.rename, sandbox_id)
                record = transition(record, "names_cut_over"); journal.write(record)
                if (await _current(record, store)).container_id != old.id:
                    raise HostedMigrationStateError("routing changed before commit")
                record = transition(record, "commit_intent"); journal.write(record)
                persistence = ({"persistence_volume": volume, "old_persistence_volume": row.persistence_volume}
                               if promotion else {})
                await store.replace_container_if_current(sandbox_id, old.id, new.id,
                                                         record["target_version"] or version, **persistence)
                committed_row = await _current(record, store)
                if committed_row.container_id != new.id:
                    raise HostedMigrationStateError("commit readback does not route to target")
                expected_row_home = volume if promotion else row.persistence_volume
                if committed_row.persistence_volume != expected_row_home:
                    raise HostedMigrationStateError("commit home readback differs from verified target")
                await _activate_promoted_target(record, target=new, store=store, client=client, journal=journal)
                return {"status": "migrated", "sandbox_id": sandbox_id, "to_image": image.id,
                        "to_version": version, "platform_env_changed": platform_env_changes}
            except Exception as exc:
                _record_error(record, journal, exc)
                outcome = await recover_hosted_migration(record, store=store, client=client, journal=journal, locked=True)
                return {**outcome, "reason": type(exc).__name__,
                        "status": "migrated" if outcome["status"] == "committed" else outcome["status"]}
    except Exception as exc:
        return {"status": _migration_failure_status(exc, admitted=record is not None), "sandbox_id": sandbox_id,
                "reason": str(exc)}
