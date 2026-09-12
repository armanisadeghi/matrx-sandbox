from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError
from orchestrator.migrate import _hosted_volume_has_other_writer


def test_journal_round_trip_is_atomic_and_fences_nonterminal(tmp_path):
    journal = HostedMigrationJournal(tmp_path / "state")
    journal.ensure_ready()
    journal.write({"sandbox_id": "sbx-a", "phase": "target_booted", "old_id": "old"})
    assert journal.inflight("sbx-a")
    assert journal.read("sbx-a")["old_id"] == "old"
    journal.write({"sandbox_id": "sbx-a", "phase": "recovered"})
    assert not journal.inflight("sbx-a")
    assert not list((tmp_path / "state").glob("*.tmp"))


def test_corrupt_journal_fails_closed(tmp_path):
    journal = HostedMigrationJournal(tmp_path / "state")
    journal.ensure_ready()
    journal.path_for("sbx-a").write_text("not-json")
    with pytest.raises(HostedMigrationStateError):
        journal.inflight("sbx-a")


def test_invalid_sandbox_id_cannot_escape_state_dir(tmp_path):
    journal = HostedMigrationJournal(tmp_path / "state")
    with pytest.raises(HostedMigrationStateError):
        journal.path_for("../outside")


def test_lock_rejects_second_admission(tmp_path):
    journal = HostedMigrationJournal(tmp_path / "state")
    with journal.lock("sbx-a"):
        with pytest.raises(HostedMigrationStateError):
            with journal.lock("sbx-a"):
                pass


@pytest.mark.asyncio
async def test_shared_volume_writer_refuses_migration():
    old = SimpleNamespace(id="old", status="running", attrs={"Mounts": [{"Name": "home"}]})
    sibling = SimpleNamespace(id="sibling", status="running", attrs={"Mounts": [{"Name": "home"}]})
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda **_: [old, sibling]))
    # docker-py's reload mutates in place; this test double only needs to keep
    # the already-observed state, proving a sibling mount fails admission.
    old.reload = lambda: None
    sibling.reload = lambda: None
    assert await _hosted_volume_has_other_writer(client, "home", "old")
