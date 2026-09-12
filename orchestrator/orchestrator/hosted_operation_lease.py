"""Shared cross-process operation fences for hosted sandbox homes.

Callers resolve the authoritative sandbox and persistent volume before entering
this lease. Migration owns the same two keys exclusively; ordinary work owns
them shared for its entire duration.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from orchestrator.config import settings
from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError


class HostedOperationDenied(RuntimeError):
    """A hosted operation overlaps a nonterminal migration or exclusive lock."""


def _pending_conflict(journal: HostedMigrationJournal, sandbox_id: str, volume: str) -> bool:
    return any(
        record.get("sandbox_id") == sandbox_id or record.get("source_volume") == volume
        for record in journal.pending()
    )


@asynccontextmanager
async def hosted_operation_lease(
    sandbox_id: str,
    volume: str,
    *,
    journal: HostedMigrationJournal | None = None,
):
    """Hold shared sandbox and home locks through one hosted operation.

    EC2 has no local Docker home volume/journal, so it deliberately remains a
    no-op. Hosted journal failure is denial: absence or corruption must never
    become permission to race a migration.
    """
    if settings.host_tier != "hosted":
        yield
        return
    if not sandbox_id or not volume:
        raise HostedOperationDenied("hosted operation requires sandbox and home volume")
    state = journal or HostedMigrationJournal()
    try:
        with state.lock(sandbox_id, shared=True), state.lock(f"volume-{volume}", shared=True):
            if _pending_conflict(state, sandbox_id, volume):
                raise HostedOperationDenied("hosted migration is pending for sandbox or home")
            yield
    except HostedMigrationStateError as exc:
        raise HostedOperationDenied("hosted operation lease unavailable") from exc
