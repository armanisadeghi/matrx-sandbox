"""Tool routes on a terminal-status sandbox must return an actionable 410.

Before this, calling /fs (etc.) on an expired box fell through to the
container-IP lookup and produced a bare 500 "Could not determine sandbox IP"
— the frontend's file tree just died with no hint that a resume would fix it.
Lifecycle routes (resume/extend/destroy) must keep working on terminal rows.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orchestrator.main import app
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore

SBX = "sbx-expired00001"


@pytest_asyncio.fixture
async def expired_store(monkeypatch):
    """Wire an in-memory store holding one EXPIRED sandbox; no API key so the
    auth middleware passes through (dev mode)."""
    from orchestrator.config import settings

    store = InMemorySandboxStore()
    await store.save(SandboxResponse(
        sandbox_id=SBX,
        user_id="00000000-0000-0000-0000-000000000001",
        organization_id="22222222-2222-4222-8222-222222222222",
        status=SandboxStatus.EXPIRED,
        created_at=datetime.now(timezone.utc),
    ))

    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: store)
    orig_key = settings.api_key
    settings.api_key = ""
    yield store
    settings.api_key = orig_key


@pytest.mark.asyncio
async def test_fs_on_expired_sandbox_is_410_with_resume_hint(expired_store):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/sandboxes/{SBX}/fs/list?path=.")
    assert resp.status_code == 410
    assert "resume" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_exec_on_expired_sandbox_is_410(expired_store):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sandboxes/{SBX}/exec", json={"command": "true"})
    assert resp.status_code == 410
