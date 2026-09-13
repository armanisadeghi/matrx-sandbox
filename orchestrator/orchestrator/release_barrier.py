"""Fail-closed release census for migration-owned Docker artifacts.

Deployment invokes this from the candidate runtime while it owns the exclusive
deployment lock.  It is deliberately read-only with respect to Docker: a
release may classify migration state, never repair or delete it.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

from docker.errors import NotFound

from orchestrator.hosted_migration import (
    TERMINAL_PHASES,
    HostedMigrationJournal,
    HostedMigrationStateError,
)
from orchestrator.hosted_backup import _volume_identity

_HELPER_PIN = re.compile(r"^matrx-migration-helper:(?P<operation>[A-Za-z0-9][A-Za-z0-9_.-]{0,200})$")
_RESERVED_CONTAINER = re.compile(r"^sbx-[0-9a-z]+-(?:mig|old)-")


def required_record_lock_keys(journal: HostedMigrationJournal) -> list[str]:
    """Return every old-source lock name implied by validated durable records."""
    keys: set[str] = set()
    for record in journal.records():
        keys.add(record["sandbox_id"])
        keys.add("volume-" + record["source_volume"])
        keys.add("volume-" + record.get("source_home_key", record["source_volume"]))
        if record.get("storage_kind") == "ec2_writable_layer":
            keys.add("copy-" + record["operation_label"])
    return sorted(keys)


def _labels(item: Any) -> dict[str, str]:
    direct = getattr(item, "labels", None)
    if isinstance(direct, dict):
        return direct
    attrs = getattr(item, "attrs", None) or {}
    value = attrs.get("Labels") or attrs.get("Config", {}).get("Labels") or {}
    return value if isinstance(value, dict) else {}


def _name(item: Any) -> str:
    value = getattr(item, "name", None)
    if isinstance(value, str) and value:
        return value.lstrip("/")
    attrs = getattr(item, "attrs", None) or {}
    return str(attrs.get("Name") or "").lstrip("/")


def audit_release_barrier(journal: HostedMigrationJournal, client: Any) -> dict[str, int]:
    """Validate clean durable state and bind every migration artifact to it."""
    records = journal.records()
    by_operation: dict[str, dict[str, Any]] = {}
    by_sandbox: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["operation_label"] in by_operation:
            raise HostedMigrationStateError("duplicate migration operation identity")
        if record["sandbox_id"] in by_sandbox:
            raise HostedMigrationStateError("duplicate migration sandbox journal")
        by_operation[record["operation_label"]] = record
        by_sandbox[record["sandbox_id"]] = record
        if record["phase"] not in TERMINAL_PHASES or not record.get("cleanup_complete"):
            raise HostedMigrationStateError(
                f"migration {record['sandbox_id']} requires recovery before release"
            )

    containers = client.containers.list(all=True)
    for container in containers:
        labels = _labels(container)
        operation = labels.get("matrx.hosted_migration")
        name = _name(container)
        if operation:
            record = by_operation.get(operation)
            if record is None:
                # One journal is retained per sandbox.  After operation B rolls
                # back, its exact old runtime can legitimately retain the
                # origin label from successful operation A.  Identity and the
                # current terminal receipt, not label recency, are the fence.
                record = next(
                    (
                        candidate
                        for candidate in records
                        if candidate["phase"] == "recovered"
                        and candidate.get("old_id") == getattr(container, "id", None)
                        and candidate["sandbox_id"] == name
                    ),
                    None,
                )
            if record is None or not (
                name == record["sandbox_id"]
                and (
                    (record["phase"] == "committed" and getattr(container, "id", None) == record.get("target_id"))
                    or (record["phase"] == "recovered" and getattr(container, "id", None) == record.get("old_id"))
                )
            ):
                raise HostedMigrationStateError(
                    "migration container differs from terminal receipt for "
                    f"{record['sandbox_id'] if record else name or getattr(container, 'id', '')}"
                )
        elif _RESERVED_CONTAINER.match(name):
            raise HostedMigrationStateError(f"unowned reserved migration container {name}")

    volumes = client.volumes.list()
    for volume in volumes:
        labels = _labels(volume)
        name = _name(volume)
        operation = labels.get("matrx.hosted_migration") or labels.get("matrx.ec2_home_copy")
        if operation:
            record = by_operation.get(operation)
            if record is None:
                # A promoted home remains the exact source volume for later
                # operations even though its creation label names operation A
                # and the single journal now contains operation B.
                record = next(
                    (candidate for candidate in records if candidate.get("source_volume") == name),
                    None,
                )
            if (
                record is None
                or name != record.get("source_volume")
                or _volume_identity(volume) != record.get("source_identity")
            ):
                raise HostedMigrationStateError(
                    "migration volume differs from terminal receipt for "
                    f"{record['sandbox_id'] if record else name}"
                )
        if labels.get("matrx.kind") == "migration-state":
            sandbox_id = labels.get("matrx.sandbox_id")
            record = by_sandbox.get(str(sandbox_id))
            if record is None or name != record.get("state_volume_name"):
                raise HostedMigrationStateError(f"unowned migration state volume {name}")
            if record["phase"] == "recovered" and not record.get("old_state_volume"):
                raise HostedMigrationStateError(
                    f"recovered migration retained unexpected state volume {name}"
                )
        elif name.startswith("matrx-migration-backup-") or name.startswith("matrx-migration-state-"):
            raise HostedMigrationStateError(f"unowned reserved migration volume {name}")

    helper_pins = 0
    for image in client.images.list(name="matrx-migration-helper"):
        for tag in getattr(image, "tags", None) or (image.attrs or {}).get("RepoTags") or ():
            match = _HELPER_PIN.fullmatch(tag)
            if not match:
                continue
            helper_pins += 1
            operation = match.group("operation")
            record = by_operation.get(operation)
            retained = (record or {}).get("cleanup_receipt", {}).get("helper_image_pin_retained")
            historical = next(
                (
                    receipt
                    for candidate in records
                    for receipt in candidate.get("retained_helper_receipts", [])
                    if receipt.get("operation") == operation
                ),
                None,
            )
            owner = retained if isinstance(retained, dict) else historical
            if not (
                isinstance(owner, dict)
                and owner.get("pin") == tag
                and owner.get("image") == getattr(image, "id", None)
                and owner.get("reason") == "last_tag_in_use"
            ):
                raise HostedMigrationStateError(f"unowned migration helper pin {tag}")

    return {
        "records": len(records),
        "containers": len(containers),
        "volumes": len(volumes),
        "helper_pins": helper_pins,
    }


def main() -> None:
    import docker

    journal = HostedMigrationJournal()
    journal.ensure_ready()
    if len(sys.argv) == 2 and sys.argv[1] == "lock-keys":
        for key in required_record_lock_keys(journal):
            print(key)
        return
    if len(sys.argv) != 1 and sys.argv[1:] != ["audit"]:
        raise SystemExit("usage: python -m orchestrator.release_barrier [audit|lock-keys]")
    receipt = audit_release_barrier(journal, docker.from_env())
    print(json.dumps({"status": "clean", **receipt}, sort_keys=True))


if __name__ == "__main__":
    main()
