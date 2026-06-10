"""Tests for per-scope tool authorization + WebSocket auth.

Covers the two isolation fixes:
  * scoped tokens must carry the scope matching the tool subpath + method
    (a fs.read token must not reach POST /fs or /exec), and
  * the WebSocket routes (/pty, /fs/watch) authenticate themselves because the
    HTTP middleware never sees WS connections.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import Headers, QueryParams

from orchestrator.auth import sandbox_token
from orchestrator.config import settings
from orchestrator.main import app
from orchestrator.middleware.auth import _required_scope_for
from orchestrator.routes.sandboxes import _authenticate_websocket

TEST_API_KEY = "test-secret-key-for-auth-tests"
TEST_TOKEN_SECRET = "test-token-signing-secret"
SBX = "sbx-abcdef123456"


@pytest.fixture
def auth_env():
    """Enable both master-key auth and scoped-token issuance."""
    orig_key, orig_secret = settings.api_key, settings.access_token_secret
    settings.api_key = TEST_API_KEY
    settings.access_token_secret = TEST_TOKEN_SECRET
    yield
    settings.api_key, settings.access_token_secret = orig_key, orig_secret


def _token(scopes: list[str], sandbox_id: str = SBX) -> str:
    token, _ = sandbox_token.issue_token(
        secret=TEST_TOKEN_SECRET, sandbox_id=sandbox_id, scopes=scopes, tier="hosted",
    )
    return token


# ── _required_scope_for mapping ──────────────────────────────────────────────

@pytest.mark.parametrize("path,method,expected", [
    ("/sandboxes/x/fs/read", "GET", "fs.read"),
    ("/sandboxes/x/fs/list", "GET", "fs.read"),
    ("/sandboxes/x/fs/write", "PUT", "fs.write"),
    ("/sandboxes/x/fs/delete", "DELETE", "fs.write"),
    ("/sandboxes/x/fs/watch", "GET", "fs.watch"),
    ("/sandboxes/x/exec", "POST", "exec.run"),
    ("/sandboxes/x/exec/stream", "POST", "exec.stream"),
    ("/sandboxes/x/search/content", "POST", "fs.read"),
    ("/sandboxes/x/processes", "GET", "processes.read"),
    ("/sandboxes/x/ports", "GET", "ports.read"),
    ("/sandboxes/x/git/status", "GET", "git"),
    ("/sandboxes/x/pty", "GET", "pty"),
])
def test_required_scope_mapping(path, method, expected):
    assert _required_scope_for(path, method) == expected


# ── Scoped-token enforcement through the HTTP middleware ─────────────────────
# A passing auth check reaches the handler, which 404s (get_sandbox mocked None
# in other tests); here the sandbox genuinely doesn't exist so a pass == 404 and
# a fail == 401. We only need to distinguish those two.

@pytest.mark.asyncio
async def test_wrong_scope_token_rejected_on_exec(auth_env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/sandboxes/{SBX}/exec",
            json={"command": "echo hi"},
            headers={"X-Sandbox-Access-Token": _token(["fs.read"])},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_right_scope_token_passes_auth_on_exec(auth_env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/sandboxes/{SBX}/exec",
            json={"command": "echo hi"},
            headers={"X-Sandbox-Access-Token": _token(["exec.run"])},
        )
    # Auth passed; handler 404s because the sandbox doesn't exist.
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_token_for_other_sandbox_rejected(auth_env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/sandboxes/{SBX}/exec",
            json={"command": "echo hi"},
            headers={"X-Sandbox-Access-Token": _token(["exec.run"], sandbox_id="sbx-other000000")},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_master_key_bypasses_scope_check(auth_env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/sandboxes/{SBX}/exec",
            json={"command": "echo hi"},
            headers={"X-API-Key": TEST_API_KEY},
        )
    assert resp.status_code == 404  # passed auth, sandbox missing


# ── WebSocket auth helper ────────────────────────────────────────────────────

class _FakeWS:
    def __init__(self, headers: dict | None = None, query: str = ""):
        self.headers = Headers(headers or {})
        self.query_params = QueryParams(query)


def test_ws_auth_dev_mode_allows_when_no_key():
    orig = settings.api_key
    settings.api_key = ""
    try:
        assert _authenticate_websocket(_FakeWS(), SBX, "pty") is True
    finally:
        settings.api_key = orig


def test_ws_auth_rejects_without_credentials(auth_env):
    assert _authenticate_websocket(_FakeWS(), SBX, "pty") is False


def test_ws_auth_accepts_master_via_query(auth_env):
    ws = _FakeWS(query=f"api_key={TEST_API_KEY}")
    assert _authenticate_websocket(ws, SBX, "pty") is True


def test_ws_auth_accepts_scoped_token_via_query(auth_env):
    ws = _FakeWS(query=f"token={_token(['pty'])}")
    assert _authenticate_websocket(ws, SBX, "pty") is True


def test_ws_auth_rejects_wrong_scope_token(auth_env):
    ws = _FakeWS(query=f"token={_token(['fs.read'])}")
    assert _authenticate_websocket(ws, SBX, "pty") is False


def test_ws_auth_rejects_token_for_other_sandbox(auth_env):
    ws = _FakeWS(query=f"token={_token(['pty'], sandbox_id='sbx-other000000')}")
    assert _authenticate_websocket(ws, SBX, "pty") is False


def test_ws_auth_consumes_single_use_token(auth_env):
    token, payload = sandbox_token.issue_token(
        secret=TEST_TOKEN_SECRET, sandbox_id=SBX, scopes=["pty"], tier="hosted",
        single_use=True,
    )
    ws = _FakeWS(query=f"token={token}")
    assert _authenticate_websocket(ws, SBX, "pty") is True
    assert sandbox_token.is_jti_consumed(payload["jti"]) is True
    # A second use of the same single-use token is rejected.
    assert _authenticate_websocket(_FakeWS(query=f"token={token}"), SBX, "pty") is False


# ── TTL ceiling ──────────────────────────────────────────────────────────────

def test_default_ttl_ceiling_clamps_to_15_min():
    _, payload = sandbox_token.issue_token(
        secret=TEST_TOKEN_SECRET, sandbox_id=SBX, scopes=["ai"], tier="hosted",
        ttl_seconds=7200,
    )
    assert payload["exp"] - payload["iat"] == sandbox_token.MAX_TTL_SECONDS


def test_server_binding_ceiling_allows_full_session():
    _, payload = sandbox_token.issue_token(
        secret=TEST_TOKEN_SECRET, sandbox_id=SBX, scopes=["ai"], tier="hosted",
        ttl_seconds=7200, max_ttl_seconds=7200,
    )
    assert payload["exp"] - payload["iat"] == 7200
