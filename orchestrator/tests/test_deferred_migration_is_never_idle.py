"""A deferred migration request is refused visibly — and polling it never says "idle".

Live evidence (2026-10-21): ``POST /sandboxes/sbx-358104000663/migrate`` with an
explicit operation id was refused 409 ``busy_deferred`` ("quiet observation
began 0s ago (< 120s)") — the quiet window starts AT the first request, by
design. The caller then polled ``GET /sandboxes/{id}/migration?operation_id=``
and was told ``idle`` for 18 minutes: nothing anywhere said the operation it
named had been refused, why, or when to come back.

Now the 409 carries ``outcome: deferred``, ``retry_after_seconds`` and a
``Retry-After`` header, and the status of that operation id says ``deferred``
with the same reason until the operation runs.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from orchestrator.main import app
from tests.test_routes import mock_sandbox_manager  # noqa: F401  (the route fixture)

OP = "c" * 32


@pytest.mark.asyncio
async def test_a_deferred_request_says_so_on_the_refusal_and_on_its_status(
    mock_sandbox_manager, monkeypatch, tmp_path
):
    from orchestrator import activity, migrate, migration_operations
    from orchestrator.hosted_migration import HostedMigrationJournal

    sandbox_id = "sbx-quiet"
    mock_sandbox_manager._get_store.return_value = object()
    monkeypatch.setattr(activity, "inflight_count", lambda _sid: 0)
    monkeypatch.setattr(activity, "open_session_count", lambda _sid: 0)
    monkeypatch.setattr(activity, "quiet_age", lambda _sid: (0.0, True))

    async def knob(_name):
        return 120

    monkeypatch.setattr(migrate, "knob_int", knob)
    monkeypatch.setattr(migration_operations, "HostedMigrationJournal", lambda: HostedMigrationJournal(tmp_path))
    migration_operations.reset_operation_registry_for_tests()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        refused = await client.post(f"/sandboxes/{sandbox_id}/migrate", params={"operation_id": OP})
        status = await client.get(f"/sandboxes/{sandbox_id}/migration", params={"operation_id": OP})

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["outcome"] == "deferred"
    assert detail["status"] == "busy_deferred"
    assert "quiet observation" in detail["reason"]
    assert detail["retry_after_seconds"] == 120
    assert refused.headers.get("Retry-After") == "120"

    body = status.json()
    assert body["outcome"] == "deferred", f"a refused operation was reported as {body['outcome']!r}"
    assert body["operation_id"] == OP
    assert "quiet observation" in body["reason"]
    migration_operations.reset_operation_registry_for_tests()
