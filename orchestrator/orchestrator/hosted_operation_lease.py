"""Shared cross-process operation fences for hosted sandbox homes.

Callers resolve the authoritative sandbox and persistent volume before entering
this lease. Migration owns the same two keys exclusively; ordinary work owns
them shared for its entire duration.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, ExitStack

from orchestrator.config import settings
from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError


class HostedOperationDenied(RuntimeError):
    """A hosted operation overlaps a nonterminal migration or exclusive lock."""


def _pending_conflict(journal: HostedMigrationJournal, sandbox_id: str, volume: str) -> bool:
    return any(
        record.get("sandbox_id") == sandbox_id or record.get("source_volume") == volume
        or record.get("source_home_key") == volume
        for record in journal.pending()
    )


@asynccontextmanager
async def hosted_operation_lease(
    sandbox_id: str,
    volume: str,
    *,
    journal: HostedMigrationJournal | None = None,
    lifecycle: bool = False,
):
    """Hold shared sandbox and home locks through one hosted operation.

    Both deployed tiers require a durable journal. EC2 writable-layer homes
    have logical layer keys until promoted to named storage. Missing/corrupt
    state never becomes permission to race a migration.
    """
    if settings.host_tier not in {"hosted", "ec2"}:
        yield
        return
    if not sandbox_id or not volume:
        raise HostedOperationDenied("hosted operation requires sandbox and home volume")
    state = journal or HostedMigrationJournal()
    try:
        with ExitStack() as locks:
            # Every operation participates in the lifecycle key: long-lived
            # proxy/PTY work holds it shared; create/destroy/reaper use the
            # exclusive mode.  This is the single compatibility boundary.
            locks.enter_context(state.lock("lifecycle-" + volume, shared=not lifecycle))
            locks.enter_context(state.lock(sandbox_id, shared=True))
            locks.enter_context(state.lock(f"volume-{volume}", shared=True))
            if _pending_conflict(state, sandbox_id, volume):
                raise HostedOperationDenied("hosted migration is pending for sandbox or home")
            yield
    except HostedMigrationStateError as exc:
        raise HostedOperationDenied("hosted operation lease unavailable") from exc
