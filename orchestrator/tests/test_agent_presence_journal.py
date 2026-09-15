"""Crash-safe presence receipts fence only destructive/exclusive operations."""
from __future__ import annotations

import pytest

from orchestrator.config import settings
from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError
from orchestrator.hosted_operation_lease import HostedOperationDenied, hosted_operation_lease_sync


SID = "sbx-presence-a"
HOME = "volume-home-a"
IDENTITY = {
    "sandbox_id": SID,
    "row_id": "11111111-1111-1111-1111-111111111111",
    "owner_id": "22222222-2222-2222-2222-222222222222",
    "container_id": "immutable-container-a",
    "home_key": HOME,
    "tier": "hosted",
}


def _record(nonce: str, *, identity: dict[str, str] = IDENTITY) -> dict:
    return {"schema_version": 1, "execution_nonce": nonce,
            "runtime_execution_id": "33333333-3333-3333-3333-333333333333",
            "state": "open", "identity": identity}


def test_presence_write_survives_ack_boundary_and_requires_exact_settlement(tmp_path) -> None:
    journal = HostedMigrationJournal(tmp_path)
    nonce = "44444444-4444-4444-8444-444444444441"
    journal.write_presence(_record(nonce))
    assert journal.unresolved_presence(SID, HOME)[0]["execution_nonce"] == nonce

    # A different nested provider may settle, but cannot erase its sibling.
    other = "44444444-4444-4444-8444-444444444442"
    journal.write_presence(_record(other))
    journal.write_presence({**journal.read_presence(other), "state": "settled", "settlement": "cancelled"})
    assert [r["execution_nonce"] for r in journal.unresolved_presence(SID, HOME)] == [nonce]


def test_open_presence_allows_shared_tool_lease_but_blocks_exclusive_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "host_tier", "hosted")
    journal = HostedMigrationJournal(tmp_path)
    journal.write_presence(_record("44444444-4444-4444-8444-444444444443"))

    # The agent must still be able to use its ordinary shared tool surface.
    with hosted_operation_lease_sync(SID, HOME, journal=journal):
        pass
    with pytest.raises(HostedOperationDenied, match="presence"):
        with hosted_operation_lease_sync(SID, HOME, journal=journal, lifecycle=True, deployment=True):
            pass


def test_presence_corruption_never_becomes_absence(tmp_path) -> None:
    journal = HostedMigrationJournal(tmp_path)
    (tmp_path / "presence-nonce-0000000004.presence").write_text("not-json")
    with pytest.raises(HostedMigrationStateError):
        journal.unresolved_presence(SID, HOME)


def test_nonce_open_and_terminal_are_immutable_idempotent_cas(tmp_path) -> None:
    journal = HostedMigrationJournal(tmp_path)
    nonce = "88888888-8888-4888-8888-888888888888"
    opening = _record(nonce)
    assert journal.open_presence(opening) == opening
    assert journal.open_presence(opening) == opening
    with pytest.raises(HostedMigrationStateError, match="conflicts"):
        journal.open_presence({**opening, "runtime_execution_id": "99999999-9999-4999-8999-999999999999"})
    settled = journal.settle_presence(nonce, identity=IDENTITY,
                                      runtime_execution_id=opening["runtime_execution_id"], settlement="completed")
    assert journal.settle_presence(nonce, identity=IDENTITY,
                                   runtime_execution_id=opening["runtime_execution_id"], settlement="completed") == settled
    with pytest.raises(HostedMigrationStateError, match="conflicts"):
        journal.settle_presence(nonce, identity=IDENTITY,
                                runtime_execution_id=opening["runtime_execution_id"], settlement="failed")
