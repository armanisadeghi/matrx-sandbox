"""Tests for API routes using FastAPI TestClient."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from orchestrator.main import app
from orchestrator.models import SandboxResponse, SandboxStatus


@pytest.fixture
def mock_sandbox_manager():
    """Mock out the sandbox_manager module used by route handlers."""
    with patch("orchestrator.routes.sandboxes.sandbox_manager") as mock:
        mock.create_sandbox = AsyncMock()
        mock.list_sandboxes = AsyncMock(return_value=[])
        mock.get_sandbox = AsyncMock(return_value=None)
        mock.exec_in_sandbox = AsyncMock()
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
async def test_request_without_key_returns_401(
    mock_sandbox_manager, mock_api_key
):
    """Request to authenticated endpoint without API key should return 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/sandboxes")

    assert response.status_code == 401
    assert "Missing API key" in response.json()["detail"]


@pytest.mark.asyncio
async def test_request_with_wrong_key_returns_403(
    mock_sandbox_manager, mock_api_key
):
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
async def test_request_with_correct_key_returns_200(
    mock_sandbox_manager, mock_api_key
):
    """Request with the correct API key should succeed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/sandboxes",
            headers={"X-API-Key": TEST_API_KEY},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_request_with_bearer_token_returns_200(
    mock_sandbox_manager, mock_api_key
):
    """Request with correct key via Authorization: Bearer should succeed."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/sandboxes",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_without_key_returns_200(
    mock_health_sandbox_manager, mock_api_key
):
    """/health should be exempt from API key auth even when key is configured."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

