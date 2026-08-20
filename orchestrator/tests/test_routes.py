"""Tests for API routes using FastAPI TestClient."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from orchestrator.main import app
from orchestrator.routes.health import _docker_container_counts


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
async def test_post_sandboxes_invalid_user_id(mock_sandbox_manager, mock_storage):
    """POST /sandboxes with an invalid user_id should return 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/sandboxes",
            json={"user_id": "invalid user id with spaces!!"},
        )
    assert response.status_code == 422


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
            json={"user_id": user_id, "template": "development", "tier": "ec2"},
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
