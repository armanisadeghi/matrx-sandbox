"""The binding is handed through Vercel to runtimes outside the EC2 VPC.

Regression from the live September 8 routing census: a private orchestrator
address must not leak into a portable client binding. An ECS consumer uses its
own configured, authenticated orchestrator endpoint for private tool transport.
"""

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from orchestrator.models import SandboxStatus
from orchestrator.routes import sandboxes
from orchestrator.auth import sandbox_token


@pytest.mark.asyncio
async def test_portable_binding_uses_public_endpoint(monkeypatch):
    sandbox = SimpleNamespace(
        sandbox_id="sbx-7ddc2eb0c364",
        row_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id="22222222-2222-2222-2222-222222222222",
        container_id="container-immutable-1",
        tier="ec2",
        hot_path="/home/agent",
        status=SandboxStatus.RUNNING,
    )

    async def get_sandbox(sandbox_id):
        return sandbox

    async def prepare_connection(sandbox):
        return None

    monkeypatch.setattr(sandboxes.sandbox_manager, "get_sandbox", get_sandbox)
    monkeypatch.setattr(
        sandboxes.sandbox_manager, "get_live_sandbox_for_issuance", get_sandbox
    )
    monkeypatch.setattr(sandboxes, "_prepare_connection", prepare_connection)
    monkeypatch.setattr(sandboxes.settings, "access_token_secret", "test-only-boundary-secret")
    monkeypatch.setattr(sandboxes.settings, "public_url", "https://sandbox-orchestrator.matrxserver.com/")
    monkeypatch.setattr(sandboxes.settings, "host_tier", "ec2")
    monkeypatch.setattr(
        sandboxes.sandbox_manager, "resolve_internal_base",
        lambda: "http://sandbox-orchestrator.internal.matrxserver.com:8000",
    )
    binding = await sandboxes.agent_binding(sandbox.sandbox_id)
    assert binding["base_url"] == (
        "https://sandbox-orchestrator.matrxserver.com/sandboxes/sbx-7ddc2eb0c364"
    )
    assert binding["root_path"] == "/home/agent"
    assert binding["access_token"]
    assert binding["agent_presence"]["protocol_version"] == 1
    assert binding["agent_presence"]["home_identity"].startswith("sha256:")
    payload = sandbox_token.verify_token(
        token=binding["access_token"], secret="test-only-boundary-secret",
        expected_sandbox_id=sandbox.sandbox_id, required_scope="agent.presence",
    )
    assert payload["actor"] == {"sandbox_owner_id": sandbox.user_id}

    monkeypatch.setattr(sandboxes.settings, "public_url", "")
    with pytest.raises(HTTPException) as refusal:
        await sandboxes.agent_binding(sandbox.sandbox_id)
    assert refusal.value.status_code == 503
    assert "MATRX_PUBLIC_URL" in refusal.value.detail
