"""Durable, fenced stop/delete operations.

This deliberately uses the migration journal root and the shared owned-task
registry.  A lifecycle receipt is a distinct ``.lifecycle`` file so migration
discovery remains unchanged, while the same filesystem locks fence a home
across orchestrator processes and restarts.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError
from orchestrator.hosted_operation_lease import HostedOperationDenied, hosted_operation_lease_sync
from orchestrator.migration_operations import active_operation, start_owned_operation

LifecycleKind = Literal["stop", "delete"]
_STATES = frozenset({"accepted", "running", "succeeded", "failed", "recovery_required"})
_ACTIVE = frozenset({"accepted", "running", "recovery_required"})
_SAFE_SANDBOX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")
_MAX_RECEIPT_BYTES = 16_384


class LifecycleConflict(RuntimeError):
    pass


class LifecycleUnavailable(RuntimeError):
    pass


def _operation_id(value: str) -> str:
    try:
        return UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError) as exc:
        raise LifecycleConflict("invalid lifecycle operation id") from exc


def _path(journal: HostedMigrationJournal, sandbox_id: str, operation_id: str) -> Path:
    if not _SAFE_SANDBOX.fullmatch(sandbox_id):
        raise LifecycleUnavailable("invalid sandbox operation identity")
    return journal.root / f"{sandbox_id}.{_operation_id(operation_id)}.lifecycle"


def _validate(record: dict[str, Any], *, sandbox_id: str | None = None,
              operation_id: str | None = None) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("schema_version") not in {1, 2, 3}:
        raise LifecycleUnavailable("lifecycle receipt is unreadable")
    required = {"schema_version", "operation_id", "sandbox_id", "row_id", "container_id", "home_key", "kind", "state", "phase"}
    allowed = required | {"reason"}
    if record["schema_version"] in {2, 3}:
        allowed |= {"graceful"}
        if not isinstance(record.get("graceful"), bool):
            raise LifecycleUnavailable("lifecycle receipt has an invalid graceful intent")
    if record["schema_version"] == 3:
        allowed |= {"tier"}
        if record.get("tier") not in {"hosted", "ec2"}:
            raise LifecycleUnavailable("lifecycle receipt has an invalid tier intent")
    if set(record) - allowed or not required <= set(record):
        raise LifecycleUnavailable("lifecycle receipt has an invalid shape")
    if record["kind"] not in {"stop", "delete"} or record["state"] not in _STATES:
        raise LifecycleUnavailable("lifecycle receipt has an invalid state")
    if not _SAFE_SANDBOX.fullmatch(record["sandbox_id"]):
        raise LifecycleUnavailable("lifecycle receipt has an invalid sandbox")
    if _operation_id(record["operation_id"]) != record["operation_id"]:
        raise LifecycleUnavailable("lifecycle receipt has an invalid operation")
    try:
        UUID(record["row_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise LifecycleUnavailable("lifecycle receipt has an invalid row") from exc
    if not all(isinstance(record[key], str) and record[key] for key in ("container_id", "home_key", "phase")):
        raise LifecycleUnavailable("lifecycle receipt is incomplete")
    if sandbox_id is not None and record["sandbox_id"] != sandbox_id:
        raise LifecycleUnavailable("lifecycle receipt targets another sandbox")
    if operation_id is not None and record["operation_id"] != _operation_id(operation_id):
        raise LifecycleUnavailable("lifecycle receipt targets another operation")
    return record


def _graceful(record: dict[str, Any]) -> bool:
    """v1 receipts predate force-stop and were exclusively graceful."""
    return record.get("graceful", True)


def _receipt_tier(record: dict[str, Any]) -> str:
    """Legacy receipts belong to this journal's admitting host, never a replacement row."""
    tier = record.get("tier")
    if tier in {"hosted", "ec2"}:
        return tier
    from orchestrator.config import settings
    return settings.host_tier


def _read(journal: HostedMigrationJournal, sandbox_id: str, operation_id: str) -> dict[str, Any] | None:
    path = _path(journal, sandbox_id, operation_id)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LifecycleUnavailable("lifecycle receipt cannot be read") from exc
    try:
        data = os.read(fd, _MAX_RECEIPT_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _MAX_RECEIPT_BYTES:
        raise LifecycleUnavailable("lifecycle receipt is too large")
    try:
        return _validate(json.loads(data), sandbox_id=sandbox_id, operation_id=operation_id)
    except (ValueError, json.JSONDecodeError) as exc:
        raise LifecycleUnavailable("lifecycle receipt is unreadable") from exc


def _write(journal: HostedMigrationJournal, record: dict[str, Any]) -> None:
    record = _validate(record)
    journal.ensure_ready()
    target = _path(journal, record["sandbox_id"], record["operation_id"])
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{record['sandbox_id']}.lifecycle.", dir=journal.root)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, encoded); os.fsync(fd)
        os.close(fd); fd = -1
        os.replace(temporary, target)
        directory = os.open(journal.root, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if fd >= 0: os.close(fd)
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _records(journal: HostedMigrationJournal) -> list[dict[str, Any]]:
    journal.ensure_ready()
    values: list[dict[str, Any]] = []
    for path in journal.root.glob("*.lifecycle"):
        parts = path.name.split(".")
        if len(parts) != 3 or parts[-1] != "lifecycle":
            raise LifecycleUnavailable("lifecycle receipt filename is invalid")
        value = _read(journal, parts[0], parts[1])
        if value is not None: values.append(value)
    return values


def _projection(record: dict[str, Any]) -> dict[str, Any]:
    out = {key: record[key] for key in ("operation_id", "sandbox_id", "row_id", "kind", "state", "phase")}
    if record.get("state") == "recovery_required": out["attention_needed"] = True
    if record.get("state") in {"failed", "recovery_required"}: out["reason"] = record.get("reason", "operation needs attention")
    return out


def _operation_lock_key(operation_id: str) -> str:
    """Global UUID fence; operation UUIDs are never target-local identities."""
    return "lifecycle-operation-" + _operation_id(operation_id)


def _global_target_conflict(journal: HostedMigrationJournal, operation_id: str, sandbox_id: str) -> None:
    """Reject a reused UUID before looking up a possibly missing new target.

    Admission takes this same lock again and retains it through its durable
    write; this short probe only gives UUID identity precedence over a target
    availability error.
    """
    with journal.lock(_operation_lock_key(operation_id)):
        for record in _records(journal):
            if record["operation_id"] == operation_id and record["sandbox_id"] != sandbox_id:
                raise LifecycleConflict("operation id was already used for another lifecycle target")


async def _runtime_is_terminal(container_id: str) -> bool:
    """Accept absence/stopped only for the immutable admitted runtime ID."""
    from orchestrator.hosted_runtime import _docker
    from orchestrator.sandbox_manager import _get_docker_client
    try:
        container = await _docker(_get_docker_client().containers.get, container_id)
    except Exception as exc:
        # Docker's concrete NotFound classes differ between the real SDK and
        # test adapters; only that named condition proves the exact runtime is
        # absent. Transport/daemon uncertainty remains fenced for recovery.
        if exc.__class__.__name__ == "NotFound":
            return True
        return False
    try:
        await _docker(container.reload)
    except Exception:
        return False
    if getattr(container, "id", None) != container_id:
        return False
    state = getattr(container, "attrs", {}).get("State", {})
    return state.get("Running") is False


def _acquire(journal: HostedMigrationJournal, record: dict[str, Any]) -> ExitStack:
    stack = ExitStack()
    try:
        journal.ensure_ready()
        # This must precede every receipt census.  A per-sandbox lock lets two
        # processes both observe no receipt and admit the same UUID elsewhere.
        stack.enter_context(journal.lock(_operation_lock_key(record["operation_id"])))
        records = _records(journal)
        for other in records:
            if other["operation_id"] == record["operation_id"] and other["sandbox_id"] != record["sandbox_id"]:
                raise LifecycleConflict("operation id was already used for another lifecycle target")
        stack.enter_context(hosted_operation_lease_sync(record["sandbox_id"], record["home_key"], journal=journal, lifecycle=True, deployment=True, lifecycle_operation_id=record["operation_id"] if _read(journal, record["sandbox_id"], record["operation_id"]) else None))
        for other in records:
            if other["operation_id"] != record["operation_id"] and other["state"] in _ACTIVE and (other["sandbox_id"] == record["sandbox_id"] or other["home_key"] == record["home_key"]):
                raise LifecycleConflict("another lifecycle operation fences this sandbox or home")
        return stack
    except Exception:
        stack.close(); raise


async def lifecycle_status(sandbox_id: str, operation_id: str, *, journal: HostedMigrationJournal | None = None) -> dict[str, Any] | None:
    state = journal or HostedMigrationJournal()
    record = await asyncio.to_thread(_read, state, sandbox_id, operation_id)
    if record is not None and record["state"] in {"accepted", "running"}:
        # The operation lock is the cross-process ownership truth.  If it is
        # free, no process can still own the admitted work: durably surface
        # attention rather than reporting a forever-running ghost.
        def orphan() -> dict[str, Any] | None:
            try:
                # The same global UUID key used by admission tells us whether
                # a different process still owns this exact operation.
                with state.lock(_operation_lock_key(record["operation_id"])):
                    fresh = _read(state, sandbox_id, record["operation_id"])
                    if fresh is None or fresh["state"] not in {"accepted", "running"}:
                        return fresh
                    attention = {**record, "state": "recovery_required", "phase": "recovery_required", "reason": "operation owner disappeared; recover the same operation"}
                    _write(state, attention)
                    return attention
            except HostedMigrationStateError:
                return None
        orphaned = await asyncio.to_thread(orphan)
        if orphaned is not None:
            record = orphaned
    return _projection(record) if record is not None else None


async def admit_lifecycle_operation(sandbox_id: str, operation_id: str, kind: LifecycleKind, *, journal: HostedMigrationJournal | None = None, recover: bool = False, graceful: bool = True) -> tuple[int, dict[str, Any]]:
    """Persist and own an exact graceful stop/delete without awaiting Docker."""
    state = journal or HostedMigrationJournal()
    operation_id = _operation_id(operation_id)
    try:
        await asyncio.to_thread(_global_target_conflict, state, operation_id, sandbox_id)
    except HostedMigrationStateError as exc:
        duplicate = await lifecycle_status(sandbox_id, operation_id, journal=state)
        duplicate_record = await asyncio.to_thread(_read, state, sandbox_id, operation_id)
        if duplicate is not None and duplicate["kind"] == kind and duplicate_record is not None and _graceful(duplicate_record) == graceful:
            return (202 if duplicate["state"] in {"accepted", "running"} else 200), duplicate
        raise LifecycleConflict("sandbox lifecycle operation conflicts") from exc
    prior = await asyncio.to_thread(_read, state, sandbox_id, operation_id)
    existing = await lifecycle_status(sandbox_id, operation_id, journal=state)
    if existing is not None:
        if existing["kind"] != kind or prior is None or _graceful(prior) != graceful:
            raise LifecycleConflict("operation id was already used for another lifecycle intent")
        if existing["state"] == "recovery_required" and not recover:
            return 200, existing
        if recover and existing["state"] in {"accepted", "running"}:
            # Its exact operation flock is still held by another owner. The
            # reconnect observes the immutable receipt; it never gets a 500
            # from attempting to steal that nonblocking lock.
            return 202, existing
        if existing["state"] in _ACTIVE and not recover:
            return 202, existing
        if existing["state"] in {"succeeded", "failed", "recovery_required"} and not recover:
            return 200, existing
        if existing["state"] in {"succeeded", "failed"}:
            return 200, existing

    # Read the target receipt only to select the immutable witness used for
    # lock acquisition.  The authoritative all-receipt census happens under
    # the global operation lock in _acquire.
    from orchestrator.home_identity import home_key
    from orchestrator.sandbox_manager import _get_store
    row = await _get_store().get(sandbox_id)
    life = await _get_store().get_lifecycle(sandbox_id)
    if (row is None or life is None or not row.container_id) and not (recover and prior is not None):
        raise LifecycleUnavailable("sandbox lifecycle target is unavailable")
    from orchestrator.config import settings
    row_tier = getattr(row.tier, "value", row.tier) if row is not None else settings.host_tier
    # A tombstone authorizes only its exact durable receipt.  A fresh UUID
    # must never gain a new destructive path through a soft-deleted row.
    if life is not None and life.get("deleted") and prior is None:
        raise LifecycleConflict("sandbox lifecycle target is deleted")
    if (not (recover and prior is not None)
            and (row is None or row_tier not in {"hosted", "ec2"} or row_tier != settings.host_tier)):
        # A foreign-tier row is canonical data owned by another orchestrator.
        # Never let a local Docker NotFound turn it into a local terminal row.
        raise LifecycleConflict("sandbox lifecycle target belongs to another tier")
    home = home_key(row) if row is not None else None
    home = home or f"layer-{sandbox_id}"
    record = prior or {
        "schema_version": 3, "operation_id": operation_id, "sandbox_id": sandbox_id,
        "row_id": str(life["row_id"]), "container_id": row.container_id, "home_key": home,
        "kind": kind, "graceful": graceful, "tier": row_tier, "state": "accepted", "phase": "admitting",
    }
    if prior is not None and (prior["kind"] != kind or _graceful(prior) != graceful):
        raise LifecycleConflict("operation id was already used for another lifecycle intent")
    acquisition = asyncio.create_task(asyncio.to_thread(_acquire, state, record))
    try:
        stack = await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        def release_when_acquired(done: asyncio.Task[ExitStack]) -> None:
            if done.cancelled() or done.exception() is not None:
                return
            asyncio.create_task(asyncio.to_thread(done.result().close))
        acquisition.add_done_callback(release_when_acquired)
        raise
    except (HostedOperationDenied, HostedMigrationStateError) as exc:
        # A held global UUID lock can be an identical request whose owner has
        # not yet returned 202.  It can never authorize a second target.
        duplicate = await lifecycle_status(sandbox_id, operation_id, journal=state)
        duplicate_record = await asyncio.to_thread(_read, state, sandbox_id, operation_id)
        if duplicate is not None and duplicate["kind"] == kind and duplicate_record is not None and _graceful(duplicate_record) == graceful:
            return (202 if duplicate["state"] in {"accepted", "running"} else 200), duplicate
        raise LifecycleConflict("sandbox lifecycle operation conflicts") from exc
    owns_stack = True
    transfer_pending = False
    try:
        # A receipt can only be recovered using its original witnesses.  Do
        # not overwrite it with a current replacement row or runtime.
        if recover and prior is not None:
            original_runtime_terminal = await _runtime_is_terminal(prior["container_id"])
            latest = await _get_store().get(sandbox_id)
            latest_life = await _get_store().get_lifecycle(sandbox_id)
            identity_matches = (
                latest is not None and latest_life is not None
                and str(latest_life.get("row_id")) == prior["row_id"]
                and latest.container_id == prior["container_id"]
                and (home_key(latest) or f"layer-{sandbox_id}") == prior["home_key"]
                and getattr(latest.tier, "value", latest.tier) == _receipt_tier(prior)
            )
            if not identity_matches:
                # The operation lock plus original lifecycle/home leases prove
                # no competing lifecycle side effect can be in flight.  Only
                # an exact original-runtime terminal census permits failed
                # terminalization; Docker uncertainty remains fenced.
                if original_runtime_terminal:
                    failed = {**prior, "state": "failed", "phase": "complete", "reason": "original lifecycle identity no longer matches"}
                    await asyncio.to_thread(_write, state, failed)
                    return 200, _projection(failed)
                attention = {**prior, "state": "recovery_required", "phase": "recovery_required", "reason": "original lifecycle identity needs recovery"}
                await asyncio.to_thread(_write, state, attention)
                return 200, _projection(attention)
            record = {**prior, "state": "running", "phase": "stopping"}
        # A recovery receipt is owned by the host that admitted it.  Current
        # row tier is an identity witness only; a replacement on another tier
        # must not prevent its original runtime census.
        if recover and prior is not None and _receipt_tier(prior) != settings.host_tier:
            raise LifecycleConflict("lifecycle recovery belongs to another tier")
        latest = await _get_store().get(sandbox_id)
        latest_life = await _get_store().get_lifecycle(sandbox_id)
        if (latest is None or latest_life is None or str(latest_life.get("row_id")) != record["row_id"]
                or latest.container_id != record["container_id"]
                or (home_key(latest) or f"layer-{sandbox_id}") != record["home_key"]
                or getattr(latest.tier, "value", latest.tier) != _receipt_tier(record)):
            raise LifecycleConflict("sandbox identity changed before lifecycle admission")
        durable_write = asyncio.create_task(asyncio.to_thread(_write, state, record))
        try:
            await asyncio.shield(durable_write)
        except asyncio.CancelledError:
            await durable_write
            await asyncio.to_thread(stack.close)
            raise
        async def work() -> dict[str, Any]:
            try:
                running = {**record, "state": "running", "phase": "stopping"}
                await asyncio.to_thread(_write, state, running)
                from orchestrator.models import SandboxStatus
                from orchestrator.sandbox_manager import _destroy_sandbox_unleased
                current = await _get_store().get(sandbox_id)
                current_life = await _get_store().get_lifecycle(sandbox_id)
                if (current is None or current_life is None
                        or str(current_life.get("row_id")) != record["row_id"]
                        or current.container_id != record["container_id"]
                        or (home_key(current) or f"layer-{sandbox_id}") != record["home_key"]
                        or getattr(current.tier, "value", current.tier) != _receipt_tier(record)):
                    raise LifecycleConflict("sandbox identity changed before side effect")
                terminal_states = {"stopped", "expired", "failed"}
                already_terminal = current_life.get("status") in terminal_states
                if not already_terminal:
                    stopped = await _destroy_sandbox_unleased(sandbox_id, _graceful(record), "user_requested", SandboxStatus.STOPPED)
                    if not stopped: raise LifecycleUnavailable("graceful stop did not complete")
                removing = {**running, "phase": "removing"}
                await asyncio.to_thread(_write, state, removing)
                if kind == "delete" and not current_life.get("deleted"):
                    if not await _get_store().soft_delete(sandbox_id):
                        raise LifecycleUnavailable("terminal row could not be deleted")
                finalizing = {**removing, "phase": "finalizing"}
                await asyncio.to_thread(_write, state, finalizing)
                checked = await _get_store().get(sandbox_id)
                checked_life = await _get_store().get_lifecycle(sandbox_id)
                if (checked is None or checked_life is None
                        or str(checked_life.get("row_id")) != record["row_id"]
                        or checked_life.get("status") not in terminal_states
                        or (kind == "delete" and not checked_life.get("deleted"))):
                    raise LifecycleUnavailable("terminal row census is incomplete")
                if not await _runtime_is_terminal(record["container_id"]):
                    raise LifecycleUnavailable("original runtime census is incomplete")
                terminal = {**finalizing, "state": "succeeded", "phase": "complete"}
                await asyncio.to_thread(_write, state, terminal)
                return _projection(terminal)
            except asyncio.CancelledError:
                attention = {**record, "state": "recovery_required", "phase": "recovery_required", "reason": "operation interrupted; recover the same operation"}
                await asyncio.shield(asyncio.to_thread(_write, state, attention))
                raise
            except Exception:
                attention = {**record, "state": "recovery_required", "phase": "recovery_required", "reason": "operation needs recovery"}
                await asyncio.to_thread(_write, state, attention)
                return _projection(attention)
            finally:
                await asyncio.to_thread(stack.close)
        # The registry's returned task is the explicit ownership-transfer
        # witness.  Until it exists this caller owns the descriptor stack;
        # after it exists only the child may close it.
        transfer_pending = True
        transfer = asyncio.create_task(start_owned_operation(sandbox_id, operation_id, work, kind="lifecycle"))
        try:
            task = await asyncio.shield(transfer)
        except asyncio.CancelledError:
            def close_only_if_untransferred(done: asyncio.Task[Any]) -> None:
                if done.cancelled() or done.exception() is not None:
                    asyncio.create_task(asyncio.to_thread(stack.close))
                    return
                if isinstance(done.result(), asyncio.Task):
                    return
                asyncio.create_task(asyncio.to_thread(stack.close))
            transfer.add_done_callback(close_only_if_untransferred)
            raise
        transfer_pending = False
        if isinstance(task, dict):
            raise LifecycleConflict("another sandbox operation is already in progress")
        owns_stack = False
        return 202, _projection(record)
    finally:
        # CancelledError is a BaseException, so this cannot rely on an
        # ``except Exception`` branch.  Before registry handoff, admission is
        # still the descriptor owner at every store/write await boundary.
        # Once handoff starts, its callback alone decides whether the child
        # accepted ownership or the stack must be released.
        if owns_stack and not transfer_pending:
            await asyncio.shield(asyncio.to_thread(stack.close))


async def wait_lifecycle_operation(sandbox_id: str, operation_id: str, *, journal: HostedMigrationJournal | None = None) -> dict[str, Any] | None:
    """Join the admitted child without making a synchronous caller own it."""
    active = active_operation(sandbox_id)
    if active is not None and active.operation_id == _operation_id(operation_id):
        try:
            return await asyncio.shield(active.task)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The child writes recovery_required before an unexpected error
            # reaches its owner; status is the durable source of truth.
            pass
    return await lifecycle_status(sandbox_id, operation_id, journal=journal)
