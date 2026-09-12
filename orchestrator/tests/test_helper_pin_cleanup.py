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
    def __init__(self, image=IMAGE, present=True):
        self.image, self.present, self.removed = image, present, []
        self.journal = None
    def get(self, name):
        if not self.present: raise NotFound("missing")
        return SimpleNamespace(id=self.image)
    def remove(self, name, noprune=False):
        assert name == PIN and noprune is True
        self.removed.append(name); self.present = False


def record(**extra):
    return {"old_id": None, "target_id": None, "backup_name": None,
            "helper_image": IMAGE, "helper_image_pin": PIN,
            "operation_label": "op-123", "cleanup_receipt": {}, **extra}


@pytest.mark.parametrize("committed", [True, False])
def test_cleanup_removes_exact_operation_tag_for_commit_and_rollback(committed):
    """Regression: helper digest stays pinned until either cleanup path is durable."""
    images, journal, state = Images(), Journal(), record()
    asyncio.run(_cleanup(state, SimpleNamespace(images=images), journal, committed=committed))
    assert images.removed == [PIN]
    assert any(w["cleanup_receipt"].get("helper_image_pin_removal_intent") == {"pin": PIN, "image": IMAGE}
               for w in journal.writes)
    assert state["cleanup_receipt"]["helper_image_pin_removed"] == PIN


def test_cleanup_refuses_missing_pin_without_durable_intent():
    with pytest.raises(HostedMigrationStateError, match="absent before durable removal intent"):
        asyncio.run(_cleanup(record(), SimpleNamespace(images=Images(present=False)), Journal(), committed=True))


def test_cleanup_refuses_retargeted_pin_without_removing_foreign_image():
    images = Images(image="sha256:" + "e" * 64)
    with pytest.raises(HostedMigrationStateError, match="no longer binds"):
        asyncio.run(_cleanup(record(), SimpleNamespace(images=images), Journal(), committed=True))
    assert images.removed == []


def test_cleanup_retry_after_crash_between_tag_remove_and_receipt_is_idempotent():
    """Regression: a removed tag with its prior durable intent must finish cleanup."""
    state = record(cleanup_receipt={"helper_image_pin_removal_intent": {"pin": PIN, "image": IMAGE}})
    images, journal = Images(present=False), Journal()
    asyncio.run(_cleanup(state, SimpleNamespace(images=images), journal, committed=True))
    assert images.removed == []
    assert state["cleanup_receipt"]["helper_image_pin_removed"] == PIN
