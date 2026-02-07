"""Tests for the sandbox manager module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.models import SandboxStatus


@pytest.fixture
def mock_docker():
    with patch("orchestrator.sandbox_manager._get_docker_client") as mock:
        client = MagicMock()
        mock.return_value = client
        yield client


@pytest.mark.asyncio
async def test_create_sandbox_generates_unique_id(mock_docker):
    """Sandbox IDs should be unique and prefixed with 'sbx-'."""
    from orchestrator.sandbox_manager import _sandboxes

    # Set up mock container
    container = MagicMock()
    container.id = "abc123"
    container.status = "running"
    container.exec_run.return_value = (0, b"")  # ready check passes
    mock_docker.containers.run.return_value = container
    mock_docker.containers.get.return_value = container

    from orchestrator import sandbox_manager

    sandbox = await sandbox_manager.create_sandbox(user_id="test-user")

    assert sandbox.sandbox_id.startswith("sbx-")
    assert sandbox.user_id == "test-user"
    assert sandbox.status == SandboxStatus.READY
    assert sandbox.container_id == "abc123"


@pytest.mark.asyncio
async def test_list_sandboxes_filters_by_user():
    """list_sandboxes should filter by user_id when provided."""
    from orchestrator import sandbox_manager
    from orchestrator.models import SandboxResponse

    from datetime import datetime, timezone

    # Seed some test data
    sandbox_manager._sandboxes = {
        "sbx-1": SandboxResponse(
            sandbox_id="sbx-1", user_id="alice", status=SandboxStatus.READY,
            created_at=datetime.now(timezone.utc),
        ),
        "sbx-2": SandboxResponse(
            sandbox_id="sbx-2", user_id="bob", status=SandboxStatus.READY,
            created_at=datetime.now(timezone.utc),
        ),
    }

    alice_sandboxes = await sandbox_manager.list_sandboxes(user_id="alice")
    assert len(alice_sandboxes) == 1
    assert alice_sandboxes[0].user_id == "alice"

    all_sandboxes = await sandbox_manager.list_sandboxes()
    assert len(all_sandboxes) == 2

    # Cleanup
    sandbox_manager._sandboxes = {}


@pytest.mark.asyncio
async def test_heartbeat_returns_false_for_unknown_sandbox():
    """heartbeat should return False for non-existent sandboxes."""
    from orchestrator import sandbox_manager
    sandbox_manager._sandboxes = {}

    result = await sandbox_manager.heartbeat("sbx-nonexistent")
    assert result is False
