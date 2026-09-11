"""Token issuance must not trust a durable row after its container vanishes."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from docker.errors import NotFound
from fastapi import HTTPException

from orchestrator import sandbox_manager
from orchestrator.models import AccessTokenRequest, SandboxResponse, SandboxStatus
from orchestrator.routes import sandboxes
from orchestrator.store import InMemorySandboxStore

SBX = "sbx-token-liveness01"
ORG = "22222222-2222-4222-8222-222222222222"
USER = "00000000-0000-0000-0000-000000000001"


def _sandbox() -> SandboxResponse:
    return SandboxResponse(
        sandbox_id=SBX,
        user_id=USER,
        organization_id=ORG,
        status=SandboxStatus.RUNNING,
        container_id="gone-container",
        created_at=datetime.now(timezone.utc),
        tier="hosted",
    )


@pytest.mark.asyncio
async def test_missing_container_is_atomically_stopped_before_token_mint(monkeypatch) -> None:
    store = InMemorySandboxStore()
    await store.save(_sandbox())

    class MissingContainers:
        def get(self, container_id):
            raise NotFound("gone")

    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    monkeypatch.setattr(
        sandbox_manager, "_get_docker_client", lambda: SimpleNamespace(containers=MissingContainers())
    )

    stale = await sandbox_manager.get_live_sandbox_for_issuance(SBX)

    assert stale is not None
    assert stale.status == SandboxStatus.STOPPED
    assert stale.stop_reason == "container_missing_at_token_issuance"


@pytest.mark.asyncio
@pytest.mark.parametrize("docker_status", ["created", "restarting"])
async def test_transitional_container_is_retryable_without_stop_transition(
    monkeypatch, docker_status: str
) -> None:
    """Startup states are alive to reconciliation, not evidence of a vanished box."""
    store = InMemorySandboxStore()
    await store.save(_sandbox())

    class TransitionalContainers:
        def get(self, container_id):
            return SimpleNamespace(status=docker_status, reload=lambda: None)

    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    monkeypatch.setattr(
        sandbox_manager,
        "_get_docker_client",
        lambda: SimpleNamespace(containers=TransitionalContainers()),
    )

    with pytest.raises(sandbox_manager.SandboxLivenessUnavailable):
        await sandbox_manager.get_live_sandbox_for_issuance(SBX)

    row = await store.get(SBX)
    assert row.status == SandboxStatus.RUNNING
    assert row.stop_reason is None


@pytest.mark.asyncio
async def test_transitional_container_returns_retryable_503_from_token_route(monkeypatch) -> None:
    async def transitional_liveness(sandbox_id):
        raise sandbox_manager.SandboxLivenessUnavailable("still restarting")

    monkeypatch.setattr(
        sandboxes.sandbox_manager, "get_live_sandbox_for_issuance", transitional_liveness
    )

    with pytest.raises(HTTPException) as refusal:
        await sandboxes.issue_access_token(SBX, AccessTokenRequest(scopes=["ai"]))

    assert refusal.value.status_code == 503
    assert refusal.value.headers["Retry-After"] == "3"


@pytest.mark.asyncio
@pytest.mark.parametrize("mint", ["access", "binding"])
async def test_stale_container_never_mints_a_token(monkeypatch, mint: str) -> None:
    stale = _sandbox()
    stale.status = SandboxStatus.STOPPED

    async def liveness_gate(sandbox_id):
        return stale

    def must_not_mint(**kwargs):
        raise AssertionError("token mint must not run for a stopped sandbox")

    monkeypatch.setattr(sandboxes.sandbox_manager, "get_live_sandbox_for_issuance", liveness_gate)
    monkeypatch.setattr(sandboxes.sandbox_token, "issue_token", must_not_mint)

    with pytest.raises(HTTPException) as refusal:
        if mint == "access":
            await sandboxes.issue_access_token(SBX, AccessTokenRequest(scopes=["ai"]))
        else:
            await sandboxes.agent_binding(SBX)

    assert refusal.value.status_code == 410
    assert "resume" in refusal.value.detail.lower()
