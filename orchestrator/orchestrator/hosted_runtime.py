"""Journaled volume-backed image replacement; never owns user row content.

The old container and verified backup survive until the database routing and
the actual target both agree. A failed/uncertain step keeps its original phase
so recovery can resume from evidence rather than treating every error alike.
"""
from __future__ import annotations

import asyncio
import copy
import re
import socket
import uuid

from docker.errors import NotFound

from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError, recovery_action, transition


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
    if endpoint.get("IPAMConfig") or host.get("NetworkMode", "").startswith("container:"):
        raise HostedMigrationStateError("static/shared network identity cannot be replaced safely")
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
        if mount.get("RW") and mount.get("Destination") != "/home/agent":
            raise HostedMigrationStateError("writable mount outside the backed-up home is unsupported")
        if mount.get("Type") == "volume" and not any(
            b.split(":", 2)[:2] == [mount.get("Name"), mount.get("Destination")]
            for b in host.get("Binds", [])
        ) and not any(m.get("Source") == mount.get("Name") and m.get("Target") == mount.get("Destination")
                      for m in host.get("Mounts", [])):
            raise HostedMigrationStateError("runtime has an implicit volume that cannot be cloned safely")
    config.update(Image=image, Env=environment, HostConfig=host)
    config["Labels"] = dict(config.get("Labels") or {}, **{"matrx.hosted_migration": operation})
    aliases = [a for a in endpoint.get("Aliases") or [] if a not in {old.id, old.id[:12]}]
    config["NetworkingConfig"] = {"EndpointsConfig": {network: {
        "Aliases": aliases, "DriverOpts": endpoint.get("DriverOpts") or {},
        "Links": endpoint.get("Links") or [],
        "MacAddress": endpoint.get("MacAddress") or "",
    }}}
    return config


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
            raise HostedMigrationStateError(
                "another running container can write this home during recovery"
            )


async def _quiesce_target(target, record):
    """Stop only a live target; Docker refuses stop-on-exited during recovery."""
    await _docker(target.reload)
    if target.status == "running":
        await _docker(target.stop, timeout=record["stop_timeout"])
        await _docker(target.reload)
    if target.status not in {"exited", "created", "dead"}:
        raise HostedMigrationStateError("target writer did not stop")


async def _ready(container, record, *, target):
    from orchestrator.migrate import _wait_container_ready, _container_version
    await _docker(container.reload)
    expected = record["target_image"] if target else record["old_image"]
    if (container.status != "running" or container.name.lstrip("/") != record["sandbox_id"]
            or container.attrs.get("Image") != expected or not _home(container, record["source_volume"])):
        return False
    if not await _wait_container_ready(container, record["verify_timeout"], record["template"]):
        return False
    return not target or not record["target_version"] or await _container_version(container) == record["target_version"]


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
            if obsolete.status == "running":
                raise HostedMigrationStateError("obsolete container still running; retain backup")
            await _docker(obsolete.remove)
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
    record["cleanup_complete"] = True
    journal.write(record)


async def recover_hosted_migration(record, *, store, client, journal, locked=False):
    """Recover only exact journal identities; DB ambiguity never authorizes restore."""
    if not locked:
        with journal.lock(record["sandbox_id"]), journal.lock("volume-" + record["source_volume"]):
            fresh = journal.read(record["sandbox_id"])
            if fresh is None:
                raise HostedMigrationStateError("recovery journal disappeared")
            return await recover_hosted_migration(fresh, store=store, client=client, journal=journal, locked=True)
    try:
        current = await _current(record, store)
        source = await _docker(client.volumes.get, record["source_volume"])
        from orchestrator.hosted_backup import _volume_identity
        if _volume_identity(source) != record["source_identity"]:
            raise HostedMigrationStateError("source volume identity changed")
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
        target_ok = target is not None and await _ready(target, record, target=True)
        action = recovery_action(record, db_container_id=current.container_id, target_exists_ready=target_ok)
        if action == "finalize_committed":
            record["phase"] = "committed"
            journal.write(record)
            await _cleanup(record, client, journal, committed=True)
            return {"status": "committed", "sandbox_id": record["sandbox_id"]}
        if action not in {"restart_old", "restore_then_restart_old"} or current.container_id != record["old_id"]:
            raise HostedMigrationStateError("database routing is ambiguous; preserve artifacts")
        old = await _get(client, record["old_id"])
        if old is None or old.attrs.get("Image") != record["old_image"] or not _home(old, record["source_volume"]):
            raise HostedMigrationStateError("original container identity changed")
        if target is not None:
            await _quiesce_target(target, record)
            if target.name.lstrip("/") == record["sandbox_id"]:
                await _docker(target.rename, record["target_name"])
        # A concurrent row deletion/change must never cause resurrection.
        if (await _current(record, store)).container_id != record["old_id"]:
            raise HostedMigrationStateError("routing changed before rollback")
        if action == "restore_then_restart_old":
            from orchestrator.hosted_backup import restore_volume
            if old.status == "running":
                await _docker(old.stop, timeout=record["stop_timeout"])
                await _docker(old.reload)
            if old.status not in {"exited", "created", "dead"}:
                raise HostedMigrationStateError("original writer did not stop before restore")
            await _assert_no_unowned_home_writer(
                client,
                record,
                allowed_ids={old.id, *( [target.id] if target is not None else [])},
            )
            record["phase"] = "restore_intent"
            journal.write(record)
            await _finish(restore_volume(client, receipt=record["backup_receipt"], image=record["helper_image"]))
        if old.name.lstrip("/") != record["sandbox_id"]:
            await _docker(old.rename, record["sandbox_id"])
        if (await _current(record, store)).container_id != old.id:
            raise HostedMigrationStateError("routing changed before original restart")
        await _docker(old.start)
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
    if settings.host_tier != "hosted":
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
                         cur, store, verify_timeout, platform_env_changes):
    """Stop, snapshot, replace, prove routing, then retire exact old artifacts."""
    from orchestrator import activity
    from orchestrator.config import settings
    from orchestrator.hosted_backup import snapshot_volume, _volume_identity
    from orchestrator.knobs import knob_int
    from orchestrator.migrate import _wait_container_ready, _container_version
    from orchestrator.sandbox_manager import _get_docker_client
    from orchestrator.versioning import _version_from_image_attrs
    if settings.host_tier != "hosted":
        return {"status": "unsupported_storage", "sandbox_id": sandbox_id,
                "reason": "this migration contract is for hosted named-volume homes only"}
    journal, client = HostedMigrationJournal(), _get_docker_client()
    record = None
    try:
        await _docker(old.reload)
        homes = [m for m in old.attrs.get("Mounts", []) if m.get("Destination") == "/home/agent"]
        if len(homes) != 1 or homes[0].get("Type") != "volume" or not homes[0].get("RW"):
            raise HostedMigrationStateError("home must be one writable named Docker volume")
        volume = homes[0]["Name"]
        with journal.lock(sandbox_id), journal.lock("volume-" + volume):
            if any((r["sandbox_id"] == sandbox_id or r["source_volume"] == volume)
                   and not r.get("cleanup_complete") for r in journal.records()):
                raise HostedMigrationStateError("an earlier migration still requires recovery")
            if activity.inflight_count(sandbox_id) or activity.open_session_count(sandbox_id):
                raise HostedMigrationStateError("sandbox still has active work or an interactive session")
            await _docker(old.reload)
            row = await store.get(sandbox_id)
            if row is None or row.container_id != old.id or old.status != "running" or not _home(old, volume):
                raise HostedMigrationStateError("original runtime no longer matches routing")
            await _assert_no_unowned_home_writer(client, {"source_volume": volume}, allowed_ids={old.id})
            image = await _docker(client.images.get, target)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image.id):
                raise HostedMigrationStateError("target image identity is not immutable")
            helper = await _get(client, socket.gethostname())
            if helper is None or not helper.id.startswith(socket.gethostname()):
                raise HostedMigrationStateError("cannot establish controlled orchestrator helper identity")
            helper_image = helper.attrs.get("Image", "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", helper_image):
                raise HostedMigrationStateError("helper image identity is not immutable")
            # Verify dependencies before stopping a user runtime. No user mounts,
            # credentials or network are exposed to this preflight helper.
            await _docker(client.containers.run, helper_image,
                command=["command -v tar getfacl sha256sum sync python3 >/dev/null"],
                entrypoint=["/bin/sh", "-ec"], network_disabled=True,
                environment={}, remove=True)
            operation = uuid.uuid4().hex
            runtime = replacement_config(old, image=image.id, environment=env, operation=operation)
            source = await _docker(client.volumes.get, volume)
            record = {
                "schema_version": 1, "sandbox_id": sandbox_id, "row_identity": _identity(row),
                "old_id": old.id, "old_image": old.attrs["Image"], "old_name": old.name.lstrip("/"),
                "source_volume": volume, "source_identity": _volume_identity(source),
                "target_name": f"{sandbox_id}-mig-{operation}", "target_image": image.id,
                "target_version": _version_from_image_attrs(image.attrs), "operation_label": operation,
                "backup_name": f"matrx-migration-backup-{operation}", "helper_image": helper_image,
                "rollback_name": f"{sandbox_id}-old-{operation}", "template": labels.get("matrx.template"),
                "verify_timeout": verify_timeout, "stop_timeout": await knob_int("shutdown_timeout_seconds"),
                "phase": "admitted",
            }
            await _current(record, store)
            journal.write(record)
            try:
                await _docker(old.stop, timeout=record["stop_timeout"])
                await _docker(old.reload)
                if old.status != "exited":
                    raise HostedMigrationStateError("original writer did not stop")
                record = transition(record, "old_stopped"); journal.write(record)
                record = transition(record, "backup_intent"); journal.write(record)
                if await _get(client, record["target_name"]) is not None:
                    raise HostedMigrationStateError("reserved target name is already occupied")
                try:
                    await _docker(client.volumes.get, record["backup_name"])
                except NotFound:
                    pass
                else:
                    raise HostedMigrationStateError("reserved backup name is already occupied")
                await _docker(client.volumes.create, record["backup_name"], labels={
                    "matrx.owner": "orchestrator", "matrx.hosted_migration": operation})
                receipt = await _finish(snapshot_volume(client, volume=volume,
                    backup_volume=record["backup_name"], image=helper_image))
                record = transition(record, "backup_verified", backup_receipt=receipt); journal.write(record)
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
                record = transition(record, "rename_intent"); journal.write(record)
                await _docker(old.rename, record["rollback_name"])
                await _docker(new.rename, sandbox_id)
                record = transition(record, "names_cut_over"); journal.write(record)
                if (await _current(record, store)).container_id != old.id:
                    raise HostedMigrationStateError("routing changed before commit")
                record = transition(record, "commit_intent"); journal.write(record)
                await store.replace_container_if_current(sandbox_id, old.id, new.id, record["target_version"] or version)
                if (await _current(record, store)).container_id != new.id or not await _ready(new, record, target=True):
                    raise HostedMigrationStateError("commit readback or live target verification failed")
                record = transition(record, "committed"); journal.write(record)
                await _cleanup(record, client, journal, committed=True)
                return {"status": "migrated", "sandbox_id": sandbox_id, "to_image": image.id,
                        "to_version": version, "platform_env_changed": platform_env_changes}
            except Exception as exc:
                _record_error(record, journal, exc)
                outcome = await recover_hosted_migration(record, store=store, client=client, journal=journal, locked=True)
                return {**outcome, "reason": type(exc).__name__,
                        "status": "migrated" if outcome["status"] == "committed" else outcome["status"]}
    except Exception as exc:
        return {"status": "recovery_required" if record else "busy_deferred", "sandbox_id": sandbox_id,
                "reason": str(exc)}
