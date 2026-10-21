"""A migration rollback always records WHY — on the op record, the receipt and a log line.

Live evidence (2026-10-21): migration op 21366a24… of sbx-358104000663 ran 14
minutes and ended ``rolled_back`` with the generic "The update was rolled
back" and nothing else. ``_record_error`` had written ``last_error`` to the
journal, then ``_cleanup`` popped it on the way to the terminal receipt, and
no line was logged: the failing step and the cause were gone everywhere.
"""

from __future__ import annotations

import copy
import logging

import pytest
from docker.errors import NotFound

from orchestrator import hosted_runtime
from orchestrator.hosted_backup import HostedBackupError
from orchestrator.hosted_migration import HostedMigrationJournal
from orchestrator.migration_operations import migration_status

from tests.test_migration_operations import record as journal_record

OP = "2" * 32


class _Gone:
    def get(self, *_a, **_k):
        raise NotFound("gone")


class _Client:
    containers = _Gone()
    volumes = _Gone()
    images = _Gone()


class _Journal:
    """Captures what the engine writes, without the phase validation."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.receipt: dict | None = None

    def write(self, rec):
        self.records.append(copy.deepcopy(rec))

    def write_operation_receipt(self, receipt):
        self.receipt = dict(receipt)


@pytest.mark.asyncio
async def test_a_rollback_keeps_the_failing_step_and_cause(caplog, tmp_path):
    rec = journal_record(operation_id=OP, phase="backup_intent")
    rec.pop("helper_image_pin", None)
    journal = _Journal()
    caplog.set_level(logging.ERROR, logger="orchestrator.hosted_runtime")

    hosted_runtime._record_error(rec, journal, HostedBackupError("backup manifest does not agree with source manifest"))
    rec["phase"] = "recovered"  # recovery resumed the original box
    await hosted_runtime._cleanup(rec, _Client(), journal, committed=False)

    receipt = journal.receipt
    assert receipt["outcome"] == "rolled_back"
    assert receipt["failed_phase"] == "backup_intent"
    assert receipt["failure_class"] == "HostedBackupError"
    assert "manifest does not agree" in receipt["failure_reason"]
    final = journal.records[-1]
    assert final["failed_phase"] == "backup_intent" and "manifest" in final["failure_reason"]
    assert any("ROLLED BACK" in r.getMessage() and "backup_intent" in r.getMessage() for r in caplog.records)

    # The status route names the step and the failure class, never raw text.
    real = HostedMigrationJournal(tmp_path)
    real.write(final)
    real.write_operation_receipt(receipt)
    status = await migration_status("sbx", OP, journal=real)
    assert status["outcome"] == "rolled_back"
    assert status["failed_phase"] == "backup_intent"
    assert status["failure_class"] == "HostedBackupError"
    assert "manifest" not in str(status)
