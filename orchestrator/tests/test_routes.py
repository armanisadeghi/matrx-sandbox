"""Tests for API routes using FastAPI TestClient."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from orchestrator.main import app
from orchestrator.routes.health import _docker_container_counts

ORG_ID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
async def test_browser_agent_proxy_preflight_allows_canonical_identity_headers():
    """The direct Code agent channel carries upstream identity and org scope.

    This request never reaches the proxy route: Starlette's CORSMiddleware
    validates the browser's requested header names first. Keep the exact
    header set emitted by ``resolveBackendForConversation`` allowed here so
    a browser receives a real response instead of a generic ``Failed to
    fetch`` before the agent request can begin.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/sandboxes/sbx-agent/proxy/v2/ai/agents/agent-id",
            headers={
                "Origin": "http://localhost:3001",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "authorization,content-type,x-fingerprint-id,x-organization-id"
                ),
            },
        )

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == "http://localhost:3001"
    assert set(response.headers["access-control-allow-headers"].lower().split(", ")) >= {
        "authorization",
        "content-type",
        "x-fingerprint-id",
        "x-organization-id",
    }


@pytest.fixture
def mock_sandbox_manager():
    """Mock out the sandbox_manager module used by route handlers."""
    with patch("orchestrator.routes.sandboxes.sandbox_manager") as mock:
        mock.create_sandbox = AsyncMock()
        mock.list_sandboxes = AsyncMock(return_value=[])
        mock.get_sandbox = AsyncMock(return_value=None)
        mock.exec_in_sandbox = AsyncMock(return_value=(0, "", "", "/home/agent"))
        mock.destroy_sandbox = AsyncMock(return_value=True)
        mock.heartbeat = AsyncMock(return_value=False)
        yield mock


@pytest.fixture
def mock_storage():
    """Mock out the storage module used by route handlers."""
    with patch("orchestrator.routes.sandboxes.storage") as mock:
        mock.ensure_user_storage = AsyncMock()
        yield mock


@pytest.fixture
def mock_health_sandbox_manager():
    """Mock out sandbox_manager for the health route."""
    with patch("orchestrator.routes.health.sandbox_manager") as mock:
        mock.list_sandboxes = AsyncMock(return_value=[])
        yield mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("busy_signal", "reason"),
    [
        ("inflight", "box has in-flight tool calls; defer migration to an idle gap"),
        ("session", "box has an open interactive session (PTY/watch); defer until it closes"),
    ],
)
async def test_migrate_route_returns_structured_conflict_without_touching_busy_sandbox(
    mock_sandbox_manager, monkeypatch, busy_signal, reason
):
    """Break caught: an active sandbox migration was emitted as an opaque 502."""
    from orchestrator import activity
    from orchestrator import sandbox_manager as manager_module

    sandbox_id = "sbx-route-busy"
    mock_sandbox_manager._get_store.return_value = object()
    monkeypatch.setattr(
        activity,
        "inflight_count",
        lambda received_id: int(busy_signal == "inflight" and received_id == sandbox_id),
    )
    monkeypatch.setattr(
        activity,
        "open_session_count",
        lambda received_id: int(busy_signal == "session" and received_id == sandbox_id),
    )

    def docker_lookup_must_not_run():
        raise AssertionError("the idle refusal must occur before any Docker lookup")

    monkeypatch.setattr(manager_module, "_get_docker_client", docker_lookup_must_not_run)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/sandboxes/{sandbox_id}/migrate")

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "status": "busy_deferred",
            "sandbox_id": sandbox_id,
            "reason": reason,
        }
    }


@pytest.mark.asyncio
async def test_migrate_route_keeps_unknown_migration_failure_loud(mock_sandbox_manager, monkeypatch):
    """Break caught: non-idle migration failures must not be downgraded to deferrals."""
    from orchestrator import migrate

    mock_sandbox_manager._get_store.return_value = object()
    failure = {"status": "recovery_required", "sandbox_id": "sbx-route-failure", "reason": "journal uncertain"}
    monkeypatch.setattr(migrate, "migrate_sandbox", AsyncMock(return_value=failure))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/sandboxes/sbx-route-failure/migrate")

    assert response.status_code == 502
    assert response.json() == {"detail": failure}


@pytest.mark.asyncio
async def test_migrate_route_forwards_confirmed_session_interruption(
    mock_sandbox_manager, monkeypatch
):
    """A confirmed Code update must reach the migration engine as an explicit opt-in."""
    from orchestrator import migrate

    mock_sandbox_manager._get_store.return_value = object()
    migrate_call = AsyncMock(
        return_value={"status": "migrated", "sandbox_id": "sbx-confirmed"}
    )
    monkeypatch.setattr(migrate, "migrate_sandbox", migrate_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/sandboxes/sbx-confirmed/migrate?interrupt_attached_sessions=true"
        )

    assert response.status_code == 200
    migrate_call.assert_awaited_once_with(
        "sbx-confirmed",
        store=mock_sandbox_manager._get_store.return_value,
        target_image=None,
        require_idle=True,
        interrupt_attached_sessions=True,
    )


@pytest.mark.asyncio
async def test_post_sandboxes_invalid_user_id(mock_sandbox_manager, mock_storage):
    """POST /sandboxes with an invalid user_id should return 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={
                "user_id": "invalid user id with spaces!!",
                "organization_id": ORG_ID,
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_sandboxes_requires_organization_before_manager(
    mock_sandbox_manager, mock_storage
):
    """A missing organization is rejected at the API boundary before any write path."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"user_id": "00000000-0000-4000-8000-000000000001"},
        )

    assert response.status_code == 422
    mock_storage.ensure_user_storage.assert_not_awaited()
    mock_sandbox_manager.create_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_development_template_requires_allowlisted_ec2_host(
    mock_sandbox_manager,
    mock_storage,
    monkeypatch,
):
    from orchestrator.config import settings

    user_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(settings, "host_tier", "ec2")
    monkeypatch.setattr(settings, "internal_development_workspace_root", "/workspace")
    monkeypatch.setattr(settings, "internal_development_user_ids", "somebody-else")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={
                "user_id": user_id,
                "organization_id": ORG_ID,
                "template": "development",
                "tier": "ec2",
            },
        )

    assert response.status_code == 403
    mock_sandbox_manager.create_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_development_template_rejects_unsafe_workspace_key(
    mock_sandbox_manager,
    mock_storage,
    monkeypatch,
):
    from orchestrator.config import settings

    user_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(settings, "host_tier", "ec2")
    monkeypatch.setattr(settings, "internal_development_workspace_root", "/workspace")
    monkeypatch.setattr(settings, "internal_development_user_ids", user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={
                "user_id": user_id,
                "organization_id": ORG_ID,
                "template": "development",
                "tier": "ec2",
                "config": {"workspace_key": "../escape"},
            },
        )

    assert response.status_code == 422
    mock_sandbox_manager.create_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_sandboxes_returns_empty_list(mock_sandbox_manager):
    """GET /sandboxes should return an empty list when no sandboxes exist."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/sandboxes")

    assert response.status_code == 200
    data = response.json()
    assert data["sandboxes"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_internal_development_template_is_visible_only_to_allowlisted_user(
    monkeypatch,
):
    from orchestrator.config import settings

    user_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(settings, "host_tier", "ec2")
    monkeypatch.setattr(settings, "internal_development_workspace_root", "/workspace")
    monkeypatch.setattr(settings, "internal_development_user_ids", user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        public_response = await client.get("/templates")
        internal_response = await client.get(f"/templates?user_id={user_id}")

    assert public_response.status_code == 200
    assert internal_response.status_code == 200
    assert "development" not in {
        item["id"] for item in public_response.json()["templates"]
    }
    assert "development" in {
        item["id"] for item in internal_response.json()["templates"]
    }


@pytest.mark.asyncio
async def test_get_sandbox_unknown_id_returns_404(mock_sandbox_manager):
    """GET /sandboxes/{id} with an unknown ID should return 404."""
    mock_sandbox_manager.get_sandbox.return_value = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/sandboxes/sbx-nonexistent")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_post_exec_unknown_sandbox_returns_404(mock_sandbox_manager):
    """POST /sandboxes/{id}/exec with an unknown ID should return 404."""
    mock_sandbox_manager.get_sandbox.return_value = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/sandboxes/sbx-nonexistent/exec",
            json={"command": "echo hello"},
        )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_health_returns_healthy(mock_health_sandbox_manager):
    """GET /health should return a healthy response."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "active_sandboxes" in data
    assert "uptime_seconds" in data


def test_system_counts_exclude_only_unclaimed_warm_pool_containers():
    def container(sandbox_id: str, *, warm: bool, status: str = "running"):
        labels = {"matrx.sandbox_id": sandbox_id}
        if warm:
            labels["matrx.warm_pool"] = "1"
        return SimpleNamespace(status=status, attrs={"Config": {"Labels": labels}})

    docker = MagicMock()
    docker.containers.list.return_value = [
        container("sbx-normal", warm=False),
        container("sbx-warm-unclaimed", warm=True),
        container("sbx-warm-claimed", warm=True),
    ]

    with patch(
        "orchestrator.routes.health.sandbox_manager._get_docker_client", return_value=docker
    ):
        counts = _docker_container_counts({"sbx-normal", "sbx-warm-claimed"})

    assert counts == {"sandbox_total": 2, "sandbox_running": 2}


# ─── API Key Authentication Tests ─────────────────────────────────────────────

TEST_API_KEY = "test-secret-key-for-auth-tests"


@pytest.fixture
def mock_api_key():
    """Temporarily set MATRX_API_KEY to enable auth enforcement."""
    from orchestrator.config import settings

    original = settings.api_key
    settings.api_key = TEST_API_KEY
    yield TEST_API_KEY
    settings.api_key = original


@pytest.mark.asyncio
async def test_request_without_key_returns_401(mock_sandbox_manager, mock_api_key):
    """Request to authenticated endpoint without API key should return 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/sandboxes")

    assert response.status_code == 401
    assert "Missing API key" in response.json()["detail"]


@pytest.mark.asyncio
async def test_request_with_wrong_key_returns_403(mock_sandbox_manager, mock_api_key):
    """Request with an incorrect API key should return 403."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/sandboxes",
            headers={"X-API-Key": "wrong-key"},
        )

    assert response.status_code == 403
    assert "Invalid API key" in response.json()["detail"]


@pytest.mark.asyncio
async def test_request_with_correct_key_returns_200(mock_sandbox_manager, mock_api_key):
    """Request with the correct API key should succeed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/sandboxes",
            headers={"X-API-Key": TEST_API_KEY},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_request_with_bearer_token_returns_200(mock_sandbox_manager, mock_api_key):
    """Request with correct key via Authorization: Bearer should succeed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/sandboxes",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_without_key_returns_200(mock_health_sandbox_manager, mock_api_key):
    """/health should be exempt from API key auth even when key is configured."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/docs", "/openapi.json", "/api-surface"])
async def test_metadata_routes_require_key_in_authenticated_mode(path, mock_api_key):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_api_surface_exposes_revision_and_filesystem_contract(mock_api_key):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api-surface",
            headers={"X-API-Key": TEST_API_KEY},
        )

    assert response.status_code == 200
    assert response.json()["source_sha"]
    assert response.json()["contracts"]["filesystem"] == 2
