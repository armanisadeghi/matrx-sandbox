"""Assembled ASGI proof for the bound-agent presence wire and replay receipt."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.auth import sandbox_token
from orchestrator.config import settings
from orchestrator.hosted_migration import HostedMigrationJournal
from orchestrator.middleware.auth import APIKeyMiddleware
from orchestrator.middleware.hosted_operation_lease import HostedOperationLeaseMiddleware
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.routes import sandboxes

SID = "sbx-presence-ws"
OWNER = "22222222-2222-2222-2222-222222222222"
SECRET = "presence-ws-secret"


@pytest.fixture
def assembled_presence(monkeypatch, tmp_path):
    sandbox = SandboxResponse(
        row_id=UUID("11111111-1111-1111-1111-111111111111"), sandbox_id=SID,
        user_id=OWNER, organization_id="33333333-3333-4333-8333-333333333333",
        status=SandboxStatus.RUNNING, container_id="immutable-container-ws",
        created_at=datetime.now(timezone.utc), tier="hosted", persistence_volume="volume-home-ws",
    )
    class Store:
        async def get(self, sandbox_id): return sandbox if sandbox_id == SID else None
    async def get_sandbox(sandbox_id): return sandbox if sandbox_id == SID else None
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr(settings, "host_tier", "hosted")
    monkeypatch.setattr(settings, "api_key", "master-key-must-not-authorize-presence")
    monkeypatch.setattr(settings, "access_token_secret", SECRET)
    monkeypatch.setattr("orchestrator.middleware.hosted_operation_lease._get_store", lambda: Store())
    monkeypatch.setattr("orchestrator.hosted_operation_lease.new_journal", lambda: journal)
    monkeypatch.setattr(sandboxes.sandbox_manager, "get_sandbox", get_sandbox)
    monkeypatch.setattr(sandboxes, "HostedMigrationJournal", lambda: journal)
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(HostedOperationLeaseMiddleware)
    app.include_router(sandboxes.router)
    return TestClient(app), sandbox, journal


def _token(*, tier="hosted", owner=OWNER, sandbox_id=SID):
    token, _ = sandbox_token.issue_token(secret=SECRET, sandbox_id=sandbox_id,
        scopes=["agent.presence"], tier=tier, actor={"sandbox_owner_id": owner})
    return token


def _descriptor(sandbox):
    return sandboxes._presence_descriptor(sandbox)


def test_assembled_ws_rejects_wrong_tier_before_durable_open(assembled_presence):
    client, sandbox, journal = assembled_presence
    with pytest.raises(Exception):
        with client.websocket_connect(f"/sandboxes/{SID}/agent-presence?token={_token(tier='ec2')}"):
            pass
    assert journal.unresolved_presence(SID, "volume-home-ws") == []


def test_assembled_ws_open_loss_then_http_same_nonce_settlement_replays(assembled_presence):
    client, sandbox, journal = assembled_presence
    nonce = "44444444-4444-4444-8444-444444444444"
    runtime = "55555555-5555-4555-8555-555555555555"
    identity = _descriptor(sandbox)
    with client.websocket_connect(f"/sandboxes/{SID}/agent-presence?token={_token()}") as ws:
        ws.send_json({"type": "open", "execution_nonce": nonce,
                      "runtime_execution_id": runtime, "identity": identity})
        ack = ws.receive_json()
        assert ack["execution_nonce"] == nonce
        assert ack["runtime_execution_id"] == runtime
        ws.close()
    assert journal.read_presence(nonce)["state"] == "open"
    body = {"runtime_execution_id": runtime, "identity": identity, "settlement": "cancelled"}
    response = client.post(f"/sandboxes/{SID}/agent-presence/{nonce}/settle",
                           json=body, headers={"X-Sandbox-Access-Token": _token()})
    assert response.status_code == 200
    assert journal.read_presence(nonce)["state"] == "settled"
    assert client.post(f"/sandboxes/{SID}/agent-presence/{nonce}/settle",
                       json=body, headers={"X-Sandbox-Access-Token": _token()}).status_code == 200
    mismatch = {**body, "settlement": "completed"}
    assert client.post(f"/sandboxes/{SID}/agent-presence/{nonce}/settle",
                       json=mismatch, headers={"X-Sandbox-Access-Token": _token()}).status_code == 409
    for invalid_identity in (
        {**identity, "protocol_version": 999},
        {key: value for key, value in identity.items() if key != "protocol_version"},
        {**identity, "unexpected": "receipt smuggling"},
    ):
        assert client.post(f"/sandboxes/{SID}/agent-presence/{nonce}/settle",
                           json={**body, "identity": invalid_identity},
                           headers={"X-Sandbox-Access-Token": _token()}).status_code == 403


def test_assembled_ws_refuses_identity_drift_before_ack(assembled_presence, monkeypatch):
    client, sandbox, journal = assembled_presence
    nonce = "66666666-6666-4666-8666-666666666666"
    runtime = "77777777-7777-4777-8777-777777777777"
    stale = _descriptor(sandbox)
    sandbox.container_id = "replaced-container"
    with pytest.raises(Exception):
        with client.websocket_connect(f"/sandboxes/{SID}/agent-presence?token={_token()}") as ws:
            ws.send_json({"type": "open", "execution_nonce": nonce,
                          "runtime_execution_id": runtime, "identity": stale})
            ws.receive_json()
    assert journal.read_presence(nonce) is None
