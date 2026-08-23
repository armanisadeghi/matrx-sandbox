"""Every orchestrator route that proxies an HTTP request to the in-container
matrx_agent daemon must forward ``X-Matrx-Agent-Token`` (via ``_with_agent``),
or the daemon's auth middleware rejects it with 401 once enforcement is on
(``MATRX_AGENT_TOKEN`` set at container spawn — the production default).

Regression coverage for the bug where ``proxy_search`` forwarded headers with
a raw dict passthrough instead of ``_with_agent(...)`` — silently dropping the
token on every ``fs_search`` tool call. The same omission was found (and
fixed) on ``proxy_credentials``, ``proxy_processes``, and ``proxy_ports``.
This test parametrizes across every HTTP proxy route so a future route added
without the wrapper fails here instead of surfacing as a live 401.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from orchestrator.config import settings
from orchestrator.main import app
from orchestrator.models import SandboxResponse, SandboxStatus

SBX = "sbx-agenttoken01"
TEST_TOKEN_SECRET = "test-agent-token-secret"
CONTAINER_IP = "10.10.10.10"


@pytest_asyncio.fixture
async def live_sandbox(monkeypatch):
    """A RUNNING sandbox with a fake container IP, no master-key auth (dev
    mode), and a real access_token_secret so agent_forward_headers mints an
    actual token instead of fail-open no-op-ing."""
    sandbox = SandboxResponse(
        sandbox_id=SBX,
        user_id="00000000-0000-0000-0000-000000000001",
        organization_id="22222222-2222-4222-8222-222222222222",
        status=SandboxStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )

    async def _get_sandbox(sid):
        return sandbox if sid == SBX else None

    async def _get_ip(sid):
        return CONTAINER_IP

    monkeypatch.setattr("orchestrator.sandbox_manager.get_sandbox", _get_sandbox)
    monkeypatch.setattr("orchestrator.sandbox_manager.get_sandbox_internal_ip", _get_ip)

    orig_key, orig_secret = settings.api_key, settings.access_token_secret
    settings.api_key = ""
    settings.access_token_secret = TEST_TOKEN_SECRET
    yield sandbox
    settings.api_key, settings.access_token_secret = orig_key, orig_secret


@pytest.fixture
def captured_headers(monkeypatch):
    """Intercept only the OUTBOUND httpx call the proxy route makes to the
    in-container daemon (identified by CONTAINER_IP) and record the headers
    it was given. Any other AsyncClient.request call — notably the test's
    own inbound ASGITransport client driving the FastAPI app — passes
    through to the real implementation untouched."""
    captured: dict[str, str] = {}
    original_request = httpx.AsyncClient.request

    async def _fake_request(self, method, url, **kwargs):
        if CONTAINER_IP in str(url):
            captured.update(kwargs.get("headers") or {})
            return httpx.Response(200, content=b"{}", request=httpx.Request(method, url))
        return await original_request(self, method, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", f"/sandboxes/{SBX}/fs/list"),
    ("GET", f"/sandboxes/{SBX}/git/status"),
    ("POST", f"/sandboxes/{SBX}/credentials"),
    ("GET", f"/sandboxes/{SBX}/search/paths"),
    ("GET", f"/sandboxes/{SBX}/processes"),
    ("GET", f"/sandboxes/{SBX}/ports"),
])
async def test_proxy_route_forwards_agent_token(live_sandbox, captured_headers, method, path):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.request(method, path)

    assert resp.status_code == 200, resp.text
    assert "X-Matrx-Agent-Token" in captured_headers
    assert captured_headers["X-Matrx-Agent-Token"]  # non-empty
