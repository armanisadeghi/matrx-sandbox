from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.hosted_migration import HostedMigrationStateError
from orchestrator.release_barrier import audit_release_barrier, required_record_lock_keys


class Collection:
    def __init__(self, values):
        self.values = values

    def list(self, **_kwargs):
        return list(self.values)


class Journal:
    def __init__(self, records):
        self._records = records

    def records(self):
        return list(self._records)


def record(**changes):
    value = {
        "sandbox_id": "sbx-one",
        "operation_label": "op-one",
        "phase": "committed",
        "cleanup_complete": True,
        "target_id": "target-one",
        "source_volume": "home-one",
        "state_volume_name": "matrx-migration-state-sbx-one",
    }
    value.update(changes)
    return value


def item(identity, name, labels=None, tags=None):
    return SimpleNamespace(
        id=identity,
        name=name,
        labels=labels or {},
        tags=tags or [],
        attrs={
            "Name": name,
            "Labels": labels or {},
            "RepoTags": tags or [],
            "Image": identity,
            "Driver": "local",
            "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
            "CreatedAt": "2026-09-12T00:00:00Z",
            "Options": {},
            "Scope": "local",
        },
    )


def client(*, containers=(), volumes=(), images=()):
    return SimpleNamespace(
        containers=Collection(containers),
        volumes=Collection(volumes),
        images=Collection(images),
    )


def test_release_census_accepts_only_artifacts_bound_to_clean_terminal_receipts():
    current = item(
        "target-one", "sbx-one", {"matrx.hosted_migration": "op-one"}
    )
    state = item(
        "state-id",
        "matrx-migration-state-sbx-one",
        {"matrx.kind": "migration-state", "matrx.sandbox_id": "sbx-one"},
    )
    receipt = audit_release_barrier(
        Journal([record()]), client(containers=[current], volumes=[state])
    )

    assert receipt == {"records": 1, "containers": 1, "volumes": 1, "helper_pins": 0}


def test_record_lock_census_derives_sandbox_home_and_ec2_copy_locks():
    named = record()
    promoted = record(
        sandbox_id="sbx-two",
        operation_label="op-two",
        source_volume="matrx-ec2-home-sbx-two",
        source_home_key="layer-sbx-two",
        storage_kind="ec2_writable_layer",
    )

    assert required_record_lock_keys(Journal([named, promoted])) == [
        "copy-op-two",
        "sbx-one",
        "sbx-two",
        "volume-home-one",
        "volume-layer-sbx-two",
        "volume-matrx-ec2-home-sbx-two",
    ]


@pytest.mark.parametrize(
    ("records", "containers", "volumes", "images", "message"),
    [
        ([record(cleanup_complete=False)], [], [], [], "requires recovery"),
        ([record()], [item("other", "sbx-one-mig-orphan")], [], [], "unowned reserved"),
        ([record()], [], [item("v", "matrx-migration-backup-orphan")], [], "unowned reserved"),
        (
            [record()],
            [],
            [],
            [item("sha256:helper", "helper", tags=["matrx-migration-helper:unknown"])],
            "unowned migration helper pin",
        ),
    ],
)
def test_release_census_fails_closed_for_pending_or_orphaned_artifacts(
    records, containers, volumes, images, message,
):
    with pytest.raises(HostedMigrationStateError, match=message):
        audit_release_barrier(
            Journal(records),
            client(containers=containers, volumes=volumes, images=images),
        )


def test_release_census_accepts_exact_retained_last_helper_pin_receipt():
    helper = item(
        "sha256:helper",
        "helper",
        tags=["matrx-migration-helper:op-one"],
    )
    clean = record(
        cleanup_receipt={
            "helper_image_pin_retained": {
                "pin": "matrx-migration-helper:op-one",
                "image": "sha256:helper",
                "reason": "last_tag_in_use",
            }
        }
    )

    receipt = audit_release_barrier(Journal([clean]), client(images=[helper]))

    assert receipt["helper_pins"] == 1


def test_release_census_accepts_prior_origin_labels_after_next_operation_rolls_back():
    latest = record(
        operation_label="op-two",
        phase="recovered",
        old_id="target-one",
        storage_kind="named_volume",
    )
    origin = item(
        "target-one", "sbx-one", {"matrx.hosted_migration": "op-one"}
    )
    promoted_home = item(
        "volume-id",
        "home-one",
        {
            "matrx.hosted_migration": "op-one",
            "matrx.ec2_home_copy": "op-one",
            "matrx.sandbox_id": "sbx-one",
        },
    )
    latest["source_identity"] = {
        "name": "home-one",
        "driver": "local",
        "created_at": "2026-09-12T00:00:00Z",
        "labels": promoted_home.attrs["Labels"],
        "scope": "local",
    }

    receipt = audit_release_barrier(
        Journal([latest]), client(containers=[origin], volumes=[promoted_home])
    )

    assert receipt["records"] == 1


def test_release_census_rejects_prior_label_on_foreign_runtime_identity():
    latest = record(operation_label="op-two", phase="recovered", old_id="target-one")
    foreign = item(
        "foreign-id", "sbx-one", {"matrx.hosted_migration": "op-one"}
    )

    with pytest.raises(HostedMigrationStateError, match="differs from terminal receipt"):
        audit_release_barrier(Journal([latest]), client(containers=[foreign]))


def test_release_census_rejects_recreated_source_volume_with_same_name():
    stale = item(
        "stale-volume",
        "home-one",
        {"matrx.hosted_migration": "old-op", "matrx.ec2_home_copy": "old-op"},
    )
    latest = record(
        operation_label="op-two",
        phase="recovered",
        source_identity={
            "name": "home-one",
            "driver": "local",
            "created_at": "different-creation",
            "labels": stale.attrs["Labels"],
            "scope": "local",
        },
    )

    with pytest.raises(HostedMigrationStateError, match="differs from terminal receipt"):
        audit_release_barrier(Journal([latest]), client(volumes=[stale]))


def test_release_census_rejects_unrecorded_helper_pin_even_on_live_control_image():
    control = item(
        "control-container",
        "matrx-orchestrator",
        {"com.docker.compose.service": "orchestrator"},
    )
    control.attrs["Image"] = "sha256:control"
    helper = item(
        "sha256:control",
        "helper",
        tags=["matrx-migration-helper:historical-op"],
    )

    with pytest.raises(HostedMigrationStateError, match="unowned migration helper pin"):
        audit_release_barrier(
            Journal([record(operation_label="op-two")]),
            client(containers=[control], images=[helper]),
        )


def test_release_census_accepts_historical_helper_pin_with_exact_carried_receipt():
    helper = item(
        "sha256:control",
        "helper",
        tags=["matrx-migration-helper:historical-op"],
    )
    latest = record(
        operation_label="op-two",
        retained_helper_receipts=[{
            "operation": "historical-op",
            "pin": "matrx-migration-helper:historical-op",
            "image": "sha256:control",
            "reason": "last_tag_in_use",
        }],
    )

    receipt = audit_release_barrier(Journal([latest]), client(images=[helper]))

    assert receipt["helper_pins"] == 1
