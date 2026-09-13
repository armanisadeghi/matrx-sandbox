"""Shared cross-process operation fences for hosted sandbox homes.

Callers resolve the authoritative sandbox and persistent volume before entering
this lease. Migration owns the same two keys exclusively; ordinary work owns
them shared for its entire duration.

Acquiring a lease is BLOCKING filesystem work (``flock`` plus the journal's
writability probe, which ``fsync``s). ``hosted_operation_lease`` is the
convenience wrapper for a single operation; anything that leases many sandboxes
in a loop MUST use :func:`hosted_operation_lease_sync` from a worker thread
(``asyncio.to_thread``) so the event loop keeps answering ``/health``. A blocked
event loop fails the container healthcheck, and Traefik then drops the only
orchestrator server from the edge — a live process returning "503 no available
server". Incident: 2026-09-13, ``docs/incidents/2026-09-13-edge-drop-reconcile.md``.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager, ExitStack
from typing import Any, Iterator, Sequence

from orchestrator.config import settings
from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError


class HostedOperationDenied(RuntimeError):
    """A hosted operation overlaps a nonterminal migration or exclusive lock."""


def new_journal() -> HostedMigrationJournal:
    """One construction point so tests can inject a journal for every caller."""
    return HostedMigrationJournal()


def _pending_conflict(
    journal: HostedMigrationJournal,
    sandbox_id: str,
    volume: str,
    pending: Sequence[dict[str, Any]] | None = None,
) -> bool:
    # ``pending`` lets a batch caller read the journal ONCE instead of once per
    # sandbox; the records are the same durable state either way.
    records = journal.pending() if pending is None else pending
    return any(
        record.get("sandbox_id") == sandbox_id or record.get("source_volume") == volume
        or record.get("source_home_key") == volume
        for record in records
    )


@contextmanager
def hosted_operation_lease_sync(
    sandbox_id: str,
    volume: str,
    *,
    journal: HostedMigrationJournal | None = None,
    lifecycle: bool = False,
    deployment: bool = False,
    pending: Sequence[dict[str, Any]] | None = None,
) -> Iterator[None]:
    """The real lease. Blocking; safe to enter from a worker thread.

    ``flock`` ownership is per open file description, not per thread, so a lease
    entered on a worker thread and released on another still fences correctly.
    """
    if settings.host_tier not in {"hosted", "ec2"}:
        yield
        return
    if not sandbox_id or not volume:
        raise HostedOperationDenied("hosted operation requires sandbox and home volume")
    state = journal or new_journal()
    try:
        # One writability probe for the whole acquisition: the four locks below
        # hit the same root microseconds apart, so probing per lock proved
        # nothing extra and cost five fsyncs per sandbox. Longer-lived callers
        # (migration steps) still re-probe on every fresh acquisition.
        with state.probed(), ExitStack() as locks:
            if lifecycle or deployment:
                locks.enter_context(state.lock("deployment", shared=True))
            # Every operation participates in the lifecycle key: long-lived
            # proxy/PTY work holds it shared; create/destroy/reaper use the
            # exclusive mode.  This is the single compatibility boundary.
            locks.enter_context(state.lock("lifecycle-" + volume, shared=not lifecycle))
            locks.enter_context(state.lock(sandbox_id, shared=True))
            locks.enter_context(state.lock(f"volume-{volume}", shared=True))
            if _pending_conflict(state, sandbox_id, volume, pending):
                raise HostedOperationDenied("hosted migration is pending for sandbox or home")
            yield
    except HostedMigrationStateError as exc:
        raise HostedOperationDenied("hosted operation lease unavailable") from exc


@asynccontextmanager
async def hosted_operation_lease(
    sandbox_id: str,
    volume: str,
    *,
    journal: HostedMigrationJournal | None = None,
    lifecycle: bool = False,
    deployment: bool = False,
):
    """Hold shared sandbox and home locks through one hosted operation.

    Both deployed tiers require a durable journal. EC2 writable-layer homes
    have logical layer keys until promoted to named storage. Missing/corrupt
    state never becomes permission to race a migration. Bounded lifecycle
    mutations also hold the deployment peer first, so graceful promotion
    cannot terminate a Docker/home mutation between validation and commit.
    Long-lived proxy, PTY, watch, and read-only reconcile leases deliberately
    omit that global peer.

    Single-operation convenience wrapper over
    :func:`hosted_operation_lease_sync`. Never call it in a fleet-sized loop —
    see this module's docstring.
    """
    with hosted_operation_lease_sync(
        sandbox_id, volume, journal=journal, lifecycle=lifecycle, deployment=deployment,
    ):
        yield
