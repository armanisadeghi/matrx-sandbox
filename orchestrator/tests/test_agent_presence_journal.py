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
    return {"schema_version": 1, "execution_nonce": nonce, "state": "open", "identity": identity}


def test_presence_write_survives_ack_boundary_and_requires_exact_settlement(tmp_path) -> None:
    journal = HostedMigrationJournal(tmp_path)
    nonce = "presence-nonce-0000000001"
    journal.write_presence(_record(nonce))
    assert journal.unresolved_presence(SID, HOME)[0]["execution_nonce"] == nonce

    # A different nested provider may settle, but cannot erase its sibling.
    other = "presence-nonce-0000000002"
    journal.write_presence(_record(other))
    journal.write_presence({**journal.read_presence(other), "state": "settled", "settlement": "cancelled"})
    assert [r["execution_nonce"] for r in journal.unresolved_presence(SID, HOME)] == [nonce]


def test_open_presence_allows_shared_tool_lease_but_blocks_exclusive_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "host_tier", "hosted")
    journal = HostedMigrationJournal(tmp_path)
    journal.write_presence(_record("presence-nonce-0000000003"))

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
