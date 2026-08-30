from __future__ import annotations

import httpx
import pytest

from matrx_tools.browser_manager import (
    BrowserManagerClient,
    BrowserManagerConfig,
    BrowserManagerNotConfiguredError,
)
from matrx_tools.session import ToolSession
from matrx_tools.tools.browser import tool_browser_navigate


def _config() -> BrowserManagerConfig:
    return BrowserManagerConfig(
        base_url="https://server.example",
        service_token="approved-server-token",
        user_id="11111111-1111-1111-1111-111111111111",
        organization_id="33333333-3333-3333-3333-333333333333",
        profile_id="22222222-2222-2222-2222-222222222222",
        execution_target="browser_fleet",
        sandbox_id="sbx-123",
    )


def test_config_fails_closed_without_explicit_browser_identity(monkeypatch):
    for key in (
        "MATRX_AIDREAM_URL",
        "MATRX_AIDREAM_SERVICE_TOKEN",
        "USER_ID",
        "ORGANIZATION_ID",
        "MATRX_BROWSER_PROFILE_ID",
        "MATRX_BROWSER_EXECUTION_TARGET",
        "SANDBOX_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(BrowserManagerNotConfiguredError, match="refuses to launch a local fallback"):
        BrowserManagerConfig.from_env()


def test_config_fails_closed_without_organization_id_specifically(monkeypatch):
    """The orchestrator already injects ORGANIZATION_ID into every sandbox
    container; a missing one is a provisioning defect this client refuses to
    paper over with a fallback organization."""
    monkeypatch.setenv("MATRX_AIDREAM_URL", "https://server.example")
    monkeypatch.setenv("MATRX_AIDREAM_SERVICE_TOKEN", "approved-server-token")
    monkeypatch.setenv("USER_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("MATRX_BROWSER_PROFILE_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("MATRX_BROWSER_EXECUTION_TARGET", "browser_fleet")
    monkeypatch.setenv("SANDBOX_ID", "sbx-123")
    monkeypatch.delenv("ORGANIZATION_ID", raising=False)

    with pytest.raises(BrowserManagerNotConfiguredError, match="ORGANIZATION_ID"):
        BrowserManagerConfig.from_env()


def test_config_succeeds_with_organization_id_set(monkeypatch):
    """Positive control for the refusal test above."""
    monkeypatch.setenv("MATRX_AIDREAM_URL", "https://server.example")
    monkeypatch.setenv("MATRX_AIDREAM_SERVICE_TOKEN", "approved-server-token")
    monkeypatch.setenv("USER_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("ORGANIZATION_ID", "33333333-3333-3333-3333-333333333333")
    monkeypatch.setenv("MATRX_BROWSER_PROFILE_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("MATRX_BROWSER_EXECUTION_TARGET", "browser_fleet")
    monkeypatch.setenv("SANDBOX_ID", "sbx-123")

    config = BrowserManagerConfig.from_env()

    assert config.organization_id == "33333333-3333-3333-3333-333333333333"
    assert config.headers()["X-Organization-Id"] == config.organization_id


@pytest.mark.asyncio
async def test_client_reuses_approved_server_auth_and_canonical_run():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/runs"):
            return httpx.Response(200, json={
                "run": {
                    "run_id": "run-1", "profile_id": _config().profile_id,
                    "state": "agent_control", "mode": "handoff_capable",
                    "execution_target": "browser_fleet", "controller_kind": "agent",
                    "controller_user_id": _config().user_id, "reconnected": False,
                    "current_url": None, "started_at": None,
                },
                "reconnected": False,
            })
        return httpx.Response(200, json={
            "ok": True, "run_id": "run-1", "active_page_id": "page-1",
            "result": {"success": True, "url": "https://example.com", "title": "Example", "http_status": 200},
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BrowserManagerClient(_config(), client=http)
        payload = await client.command({
            "command": "navigate", "url": "https://example.com",
            "wait_until": "domcontentloaded", "timeout_ms": 30_000,
            "extract_text": False,
        })

    assert payload["active_page_id"] == "page-1"
    assert len(requests) == 2
    assert requests[0].headers["authorization"] == "Bearer approved-server-token"
    assert requests[0].headers["x-matrx-user-id"] == _config().user_id
    assert requests[0].headers["x-organization-id"] == _config().organization_id
    start = __import__("json").loads(requests[0].content)
    assert start["profile_id"] == _config().profile_id
    assert start["execution_target"] == "browser_fleet"
    assert start["activation_key"] == "sandbox:sbx-123"


@pytest.mark.asyncio
async def test_existing_tool_schema_routes_to_manager_without_playwright():
    class FakeClient:
        current_url = "https://example.com"

        async def command(self, command):
            assert command["command"] == "navigate"
            return {
                "ok": True,
                "run_id": "run-1",
                "active_page_id": "page-1",
                "result": {"url": "https://example.com", "title": "Example", "http_status": 200},
            }

        async def close(self):
            return None

    session = ToolSession()
    session.browser = FakeClient()
    result = await tool_browser_navigate(session, "https://example.com")

    assert result.type.value == "success"
    assert "Status: 200" in result.output
    assert result.metadata == {"run_id": "run-1", "page_id": "page-1"}
