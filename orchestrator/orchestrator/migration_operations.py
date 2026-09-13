"""In-process ownership for durable sandbox migration tasks.

The journal remains the crash authority. This registry answers only whether
this process currently owns an exact operation, and shields that work from a
client disconnect canceling its HTTP waiter.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from orchestrator.hosted_migration import (
    HostedMigrationJournal,
    HostedMigrationStateError,
)


OperationKind = Literal["migrating", "recovering"]


@dataclass(frozen=True)
class ActiveMigrationOperation:
    sandbox_id: str
    operation_id: str
    kind: OperationKind
    task: asyncio.Task[dict[str, Any]]


_operations: dict[str, ActiveMigrationOperation] = {}
_registry_lock: asyncio.Lock | None = None
_registry_loop: asyncio.AbstractEventLoop | None = None


def _lock() -> asyncio.Lock:
    global _registry_lock, _registry_loop
    loop = asyncio.get_running_loop()
    if _registry_lock is None or _registry_loop is not loop:
        _registry_lock = asyncio.Lock()
        _registry_loop = loop
    return _registry_lock


def active_operation(sandbox_id: str) -> ActiveMigrationOperation | None:
    entry = _operations.get(sandbox_id)
    if entry is None or entry.task.done():
        return None
    return entry


def _status_reason(outcome: str) -> str | None:
    if outcome == "rolled_back":
        return "The update was rolled back; the original sandbox is available."
    if outcome == "recovery_required":
        return "The update outcome requires orchestrator recovery. Do not start another update."
    return None


async def migration_status(
    sandbox_id: str,
    operation_id: str | None = None,
    *,
    journal: HostedMigrationJournal | None = None,
) -> dict[str, Any]:
    """Return a bounded status projection; journal contents never leave the host."""
    state = journal or HostedMigrationJournal()
    try:
        # Journal writes use atomic replace. A status request must not wait for
        # the long-lived migration lock or block the event loop on a large
        # verified-home manifest.
        record, receipt = await asyncio.to_thread(
            lambda: (
                state.read(sandbox_id),
                state.read_operation_receipt(sandbox_id),
            )
        )
    except HostedMigrationStateError:
        return {
            "sandbox_id": sandbox_id,
            "operation_id": operation_id,
            "outcome": "recovery_required",
            "execution_state": "unowned",
            "phase": "unreadable",
            "reason": _status_reason("recovery_required"),
        }

    active = active_operation(sandbox_id)
    if operation_id is not None:
        if active is not None and active.operation_id != operation_id:
            active = None
        if record is not None and record.get("operation_label") != operation_id:
            record = None
        if receipt is not None and receipt.get("operation_id") != operation_id:
            receipt = None

    if active is not None:
        active_record = (
            record
            if record is not None
            and record.get("operation_label") == active.operation_id
            else None
        )
        return {
            "sandbox_id": sandbox_id,
            "operation_id": active.operation_id,
            "outcome": "recovering" if active.kind == "recovering" else "in_progress",
            "execution_state": "running",
            "phase": (
                active_record.get("phase", "admitting")
                if active_record is not None
                else "admitting"
            ),
        }

    if record is None and receipt is not None:
        outcome = receipt["outcome"]
        response = {
            "sandbox_id": sandbox_id,
            "operation_id": receipt["operation_id"],
            "outcome": outcome,
            "execution_state": "complete",
            "phase": receipt["phase"],
        }
        reason = _status_reason(outcome)
        if reason:
            response["reason"] = reason
        return response

    if record is None:
        return {
            "sandbox_id": sandbox_id,
            "operation_id": operation_id,
            "outcome": "idle",
            "execution_state": "none",
            "phase": "idle",
        }

    if (
        operation_id is None
        and record.get("phase") in {"committed", "recovered"}
        and record.get("cleanup_complete") is True
    ):
        return {
            "sandbox_id": sandbox_id,
            "operation_id": None,
            "outcome": "idle",
            "execution_state": "none",
            "phase": "idle",
        }

    phase = record["phase"]
    cleanup_complete = record.get("cleanup_complete") is True
    if phase == "committed" and cleanup_complete:
        outcome = "migrated"
        execution_state = "complete"
    elif phase == "recovered" and cleanup_complete:
        outcome = "rolled_back"
        execution_state = "complete"
    else:
        outcome = "recovery_required"
        execution_state = "unowned"
    response = {
        "sandbox_id": sandbox_id,
        "operation_id": record["operation_label"],
        "outcome": outcome,
        "execution_state": execution_state,
        "phase": phase,
    }
    reason = _status_reason(outcome)
    if reason:
        response["reason"] = reason
    receipt = record.get("rollback_readiness_receipt")
    if outcome == "rolled_back" and isinstance(receipt, dict):
        if receipt.get("template_service_ready") is False:
            if receipt.get("baseline_template_service_ready") is False:
                response["baseline_degraded"] = True
            elif receipt.get("baseline_template_service_ready") is None:
                response["baseline_unknown"] = True
    return response


async def record_terminal_operation(
    sandbox_id: str,
    operation_id: str,
    *,
    outcome: Literal["migrated", "rolled_back"],
    phase: str,
    journal: HostedMigrationJournal | None = None,
) -> None:
    """Persist an exact terminal result that has no full phase-engine record."""
    state = journal or HostedMigrationJournal()

    def write() -> None:
        state.ensure_ready()
        with state.lock(sandbox_id):
            state.write_operation_receipt({
                "schema_version": 1,
                "sandbox_id": sandbox_id,
                "operation_id": operation_id,
                "outcome": outcome,
                "phase": phase,
            })

    await asyncio.to_thread(write)


async def run_owned_operation(
    sandbox_id: str,
    operation_id: str,
    factory: Callable[[], Awaitable[dict[str, Any]]],
    *,
    kind: OperationKind = "migrating",
) -> dict[str, Any]:
    """Run/share one exact operation without tying it to an HTTP waiter."""
    async with _lock():
        current = active_operation(sandbox_id)
        if current is not None:
            if current.operation_id != operation_id:
                return {
                    "status": "busy_deferred",
                    "sandbox_id": sandbox_id,
                    "operation_id": operation_id,
                    "reason": "another sandbox image operation is already in progress",
                }
            task = current.task
        else:
            task = asyncio.create_task(factory())
            entry = ActiveMigrationOperation(sandbox_id, operation_id, kind, task)
            _operations[sandbox_id] = entry

            def remove_if_current(_finished: asyncio.Task[dict[str, Any]]) -> None:
                if _operations.get(sandbox_id) is entry:
                    _operations.pop(sandbox_id, None)
                # A disconnected HTTP waiter may never retrieve a terminal
                # task exception. Consume it here after preserving journal
                # state so asyncio does not turn that into a second, context-
                # free "Task exception was never retrieved" symptom.
                if not _finished.cancelled():
                    _finished.exception()

            task.add_done_callback(remove_if_current)

    result = await asyncio.shield(task)
    return {**result, "operation_id": operation_id}


def reset_operation_registry_for_tests() -> None:
    """Test isolation only; production lifecycle never clears live ownership."""
    global _registry_lock, _registry_loop
    if any(not entry.task.done() for entry in _operations.values()):
        raise RuntimeError("cannot reset a registry with live migration tasks")
    _operations.clear()
    _registry_lock = None
    _registry_loop = None


async def drain_owned_operations(*, kind: OperationKind | None = None) -> None:
    """Finish shielded children before their Docker/store resources close."""
    while True:
        tasks = [
            entry.task
            for entry in tuple(_operations.values())
            if not entry.task.done() and (kind is None or entry.kind == kind)
        ]
        if not tasks:
            return
        await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
