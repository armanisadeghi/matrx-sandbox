"""Crash-safe operation helper-tag cleanup contracts."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from docker.errors import NotFound

from orchestrator.hosted_migration import HostedMigrationStateError
from orchestrator.hosted_runtime import _cleanup


IMAGE = "sha256:" + "d" * 64
PIN = "matrx-migration-helper:op-123"


class Journal:
    def __init__(self): self.writes = []
    def write(self, record): self.writes.append(copy.deepcopy(record))


class Images:
    def __init__(self, image=IMAGE, present=True, tags=None):
        self.image, self.present, self.removed = image, present, []
        self.tags = tags or [PIN, "matrx-orchestrator:latest"]
        self.journal = None
    def get(self, name):
        if not self.present: raise NotFound("missing")
        return SimpleNamespace(id=self.image, tags=self.tags, attrs={"RepoTags": self.tags})
    def remove(self, name, noprune=False, force=False):
        assert name == PIN and noprune is True
        self.removed.append((name, force)); self.present = False


class Containers:
    def __init__(self, references=None): self.references = references or []
    def list(self, **kwargs):
        assert kwargs == {"all": True, "filters": {"ancestor": IMAGE}}
        return self.references


def client(images, references=None):
    return SimpleNamespace(images=images, containers=Containers(references))


def record(**extra):
    return {"old_id": None, "target_id": None, "backup_name": None,
            "helper_image": IMAGE, "helper_image_pin": PIN,
            "operation_label": "op-123", "cleanup_receipt": {}, **extra}


@pytest.mark.parametrize("committed", [True, False])
def test_cleanup_removes_exact_operation_tag_for_commit_and_rollback(committed):
    """Regression: a verified helper alias is removed without force on either cleanup path."""
    images, journal, state = Images(), Journal(), record()
    asyncio.run(_cleanup(state, client(images), journal, committed=committed))
    assert images.removed == [(PIN, False)]
    assert any(w["cleanup_receipt"].get("helper_image_pin_removal_intent") == {"pin": PIN, "image": IMAGE}
               for w in journal.writes)
    assert state["cleanup_receipt"]["helper_image_pin_removed"] == PIN


def test_successful_cleanup_clears_stale_recovery_error():
    """A recovered journal must not retain the cleanup failure that recovery resolved."""
    images, journal = Images(), Journal()
    state = record(last_error="APIError: helper tag is in use")
    asyncio.run(_cleanup(state, client(images), journal, committed=True))
    assert state["cleanup_complete"] is True
    assert "last_error" not in state
    assert "last_error" not in journal.writes[-1]


def test_cleanup_refuses_missing_pin_without_durable_intent():
    with pytest.raises(HostedMigrationStateError, match="absent before durable removal intent"):
        asyncio.run(_cleanup(record(), client(Images(present=False)), Journal(), committed=True))


def test_cleanup_refuses_retargeted_pin_without_removing_foreign_image():
    images = Images(image="sha256:" + "e" * 64)
    with pytest.raises(HostedMigrationStateError, match="no longer binds"):
        asyncio.run(_cleanup(record(), client(images), Journal(), committed=True))
    assert images.removed == []


def test_cleanup_retry_after_crash_between_tag_remove_and_receipt_is_idempotent():
    """Regression: a removed tag with its prior durable intent must finish cleanup."""
    state = record(cleanup_receipt={"helper_image_pin_removal_intent": {"pin": PIN, "image": IMAGE}})
    images, journal = Images(present=False), Journal()
    asyncio.run(_cleanup(state, client(images), journal, committed=True))
    assert images.removed == []
    assert state["cleanup_receipt"]["helper_image_pin_removed"] == PIN


def test_cleanup_retains_the_last_tag_while_its_image_is_in_use():
    """The orchestrator must remain restartable when its helper tag is its only image name."""
    images, journal, state = Images(tags=[PIN]), Journal(), record()
    asyncio.run(_cleanup(state, client(images, [SimpleNamespace(id="running")]), journal, committed=True))
    assert images.removed == []
    assert state["cleanup_receipt"]["helper_image_pin_retained"] == {
        "pin": PIN, "image": IMAGE, "reason": "last_tag_in_use",
    }
    assert state["cleanup_complete"] is True


def test_cleanup_deletes_an_unused_image_when_the_helper_pin_is_its_last_tag():
    images, journal, state = Images(tags=[PIN]), Journal(), record()
    asyncio.run(_cleanup(state, client(images), journal, committed=True))
    assert images.removed == [(PIN, False)]
    assert state["cleanup_receipt"]["helper_image_pin_removed"] == PIN


def test_cleanup_refuses_a_tag_inventory_that_does_not_contain_the_pin():
    images = Images(tags=["matrx-orchestrator:latest"])
    with pytest.raises(HostedMigrationStateError, match="not present in its image tag inventory"):
        asyncio.run(_cleanup(record(), client(images), Journal(), committed=True))
    assert images.removed == []
