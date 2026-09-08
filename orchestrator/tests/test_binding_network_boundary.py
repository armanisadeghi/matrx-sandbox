"""The binding is handed through Vercel to runtimes outside the EC2 VPC.

Regression from the live September 8 routing census: a private orchestrator
address must not leak into a portable client binding. An ECS consumer uses its
own configured, authenticated orchestrator endpoint for private tool transport.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from orchestrator.routes import sandboxes


@pytest.mark.asyncio
async def test_portable_binding_uses_public_endpoint(monkeypatch):
    sandbox = SimpleNamespace(
        sandbox_id="sbx-7ddc2eb0c364", tier="ec2", hot_path="/home/agent"
    )

    async def get_sandbox(sandbox_id):
        return sandbox

    async def prepare_connection(sandbox):
        return None

    monkeypatch.setattr(sandboxes.sandbox_manager, "get_sandbox", get_sandbox)
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

    monkeypatch.setattr(sandboxes.settings, "public_url", "")
    with pytest.raises(HTTPException) as refusal:
        await sandboxes.agent_binding(sandbox.sandbox_id)
    assert refusal.value.status_code == 503
    assert "MATRX_PUBLIC_URL" in refusal.value.detail
