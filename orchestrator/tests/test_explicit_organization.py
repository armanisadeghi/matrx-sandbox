"""Organization identity must be explicit before sandbox persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from orchestrator.models import CreateSandboxRequest, SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore, PostgresSandboxStore

USER_ID = "11111111-1111-4111-8111-111111111111"
ORG_ID = "22222222-2222-4222-8222-222222222222"


def _sandbox(**updates) -> SandboxResponse:
    values = {
        "sandbox_id": "sbx-explicit-org",
        "user_id": USER_ID,
        "organization_id": ORG_ID,
        "status": SandboxStatus.READY,
        "created_at": datetime.now(UTC),
    }
    values.update(updates)
    return SandboxResponse(**values)


def test_create_request_rejects_missing_organization():
    with pytest.raises(ValidationError, match="organization_id"):
        CreateSandboxRequest(user_id=USER_ID)


def test_persistent_model_rejects_missing_organization():
    with pytest.raises(ValidationError, match="organization_id"):
        SandboxResponse(
            sandbox_id="sbx-missing-org",
            user_id=USER_ID,
            status=SandboxStatus.READY,
            created_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_both_stores_reject_missing_organization_before_storage(monkeypatch):
    # model_copy intentionally bypasses field validation to prove each store
    # independently refuses a corrupted/mutated model at its write boundary.
    corrupted = _sandbox().model_copy(update={"organization_id": None})

    memory = InMemorySandboxStore()
    with pytest.raises(ValueError, match="explicit organization_id"):
        await memory.save(corrupted)
    assert await memory.get(corrupted.sandbox_id) is None

    postgres = PostgresSandboxStore("postgresql://unused")
    get_pool = AsyncMock()
    monkeypatch.setattr(postgres, "_get_pool", get_pool)
    with pytest.raises(ValueError, match="explicit organization_id"):
        await postgres.save(corrupted)
    get_pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_manager_rejects_conflicting_config_organization(monkeypatch):
    from orchestrator import sandbox_manager

    save = AsyncMock()
    monkeypatch.setattr(
        sandbox_manager, "_get_store", lambda: SimpleNamespace(save=save)
    )

    with pytest.raises(ValueError, match="must match"):
        await sandbox_manager.create_sandbox(
            user_id=USER_ID,
            organization_id=ORG_ID,
            config={"organization_id": "33333333-3333-4333-8333-333333333333"},
        )

    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_insert_carries_explicit_organization():
    calls: list[tuple[str, tuple]] = []

    class Connection:
        async def execute(self, sql, *args):
            calls.append((sql, args))

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    store = PostgresSandboxStore("postgresql://unused")
    store._pool = Pool()
    await store.save(_sandbox())

    assert len(calls) == 1
    sql, args = calls[0]
    assert "(user_id, organization_id, sandbox_id" in sql
    assert "organization_id = EXCLUDED.organization_id" in sql
    assert args[1] == UUID(ORG_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reset_sandbox", "resume_sandbox"])
async def test_recreate_lifecycle_preserves_model_organization(monkeypatch, operation):
    from orchestrator.routes import sandboxes as routes

    old = _sandbox(
        status=(
            SandboxStatus.STOPPED
            if operation == "resume_sandbox"
            else SandboxStatus.READY
        ),
        config={},
    )
    create = AsyncMock(return_value=old)
    store = SimpleNamespace(
        get_lifecycle=AsyncMock(return_value={"deleted": False, "status": "stopped"})
    )

    monkeypatch.setattr(
        routes.sandbox_manager, "get_sandbox", AsyncMock(return_value=old)
    )
    monkeypatch.setattr(routes.sandbox_manager, "create_sandbox", create)
    monkeypatch.setattr(routes.sandbox_manager, "_get_store", lambda: store)
    monkeypatch.setattr(
        routes.sandbox_manager, "destroy_sandbox", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(routes.storage, "ensure_user_storage", AsyncMock())

    await getattr(routes, operation)(old.sandbox_id)

    assert create.await_args.kwargs["organization_id"] == ORG_ID
