"""Tests for the sandbox manager module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore


@pytest.fixture(autouse=True)
def clean_sandbox_state():
    """Reset sandbox manager state before and after each test.

    Injects a fresh InMemorySandboxStore so tests don't rely on
    the old _sandboxes dict (which no longer exists).
    """
    from orchestrator import sandbox_manager

    store = InMemorySandboxStore()
    sandbox_manager._store = store
    sandbox_manager._docker_client = None
    yield store
    sandbox_manager._store = None
    sandbox_manager._docker_client = None


@pytest.fixture
def mock_docker():
    with patch("orchestrator.sandbox_manager._get_docker_client") as mock:
        client = MagicMock()
        mock.return_value = client
        yield client


@pytest.mark.asyncio
async def test_create_sandbox_generates_unique_id(mock_docker):
    """Sandbox IDs should be unique and prefixed with 'sbx-'."""
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
async def test_list_sandboxes_filters_by_user(clean_sandbox_state):
    """list_sandboxes should filter by user_id when provided."""
    from orchestrator import sandbox_manager

    store = clean_sandbox_state

    # Seed some test data through the store
    await store.save(SandboxResponse(
        sandbox_id="sbx-1", user_id="alice", status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc),
    ))
    await store.save(SandboxResponse(
        sandbox_id="sbx-2", user_id="bob", status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc),
    ))

    alice_sandboxes = await sandbox_manager.list_sandboxes(user_id="alice")
    assert len(alice_sandboxes) == 1
    assert alice_sandboxes[0].user_id == "alice"

    all_sandboxes = await sandbox_manager.list_sandboxes()
    assert len(all_sandboxes) == 2


@pytest.mark.asyncio
async def test_heartbeat_returns_false_for_unknown_sandbox():
    """heartbeat should return False for non-existent sandboxes."""
    from orchestrator import sandbox_manager

    result = await sandbox_manager.heartbeat("sbx-nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_heartbeat_returns_true_for_known_sandbox(clean_sandbox_state):
    """heartbeat should return True when the sandbox exists."""
    from orchestrator import sandbox_manager

    store = clean_sandbox_state
    await store.save(SandboxResponse(
        sandbox_id="sbx-known", user_id="alice", status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc),
    ))

    result = await sandbox_manager.heartbeat("sbx-known")
    assert result is True


@pytest.mark.asyncio
async def test_exec_in_sandbox_not_running(mock_docker, clean_sandbox_state):
    """exec_in_sandbox should raise RuntimeError when the container is not running."""
    from orchestrator import sandbox_manager

    store = clean_sandbox_state
    await store.save(SandboxResponse(
        sandbox_id="sbx-stopped", user_id="alice", status=SandboxStatus.READY,
        container_id="container-stopped",
        created_at=datetime.now(timezone.utc),
    ))

    container = MagicMock()
    container.status = "exited"
    mock_docker.containers.get.return_value = container

    with pytest.raises(RuntimeError, match="is not running"):
        await sandbox_manager.exec_in_sandbox("sbx-stopped", "echo hello")


@pytest.mark.asyncio
async def test_exec_in_sandbox_command_too_long(mock_docker, clean_sandbox_state):
    """exec_in_sandbox should raise ValueError when command exceeds max length."""
    from orchestrator import sandbox_manager
    from orchestrator.config import settings

    store = clean_sandbox_state
    await store.save(SandboxResponse(
        sandbox_id="sbx-long", user_id="alice", status=SandboxStatus.READY,
        container_id="container-long",
        created_at=datetime.now(timezone.utc),
    ))

    long_command = "x" * (settings.max_command_length + 1)

    with pytest.raises(ValueError, match="exceeds max length"):
        await sandbox_manager.exec_in_sandbox("sbx-long", long_command)


@pytest.mark.asyncio
async def test_destroy_sandbox_marks_stopped(mock_docker, clean_sandbox_state):
    """destroy_sandbox should mark the sandbox as stopped (not delete it)."""
    from orchestrator import sandbox_manager

    store = clean_sandbox_state
    await store.save(SandboxResponse(
        sandbox_id="sbx-destroy", user_id="alice", status=SandboxStatus.READY,
        container_id="container-destroy",
        created_at=datetime.now(timezone.utc),
    ))

    container = MagicMock()
    mock_docker.containers.get.return_value = container

    result = await sandbox_manager.destroy_sandbox("sbx-destroy")

    assert result is True
    stopped = await store.get("sbx-destroy")
    assert stopped is not None
    assert stopped.status == SandboxStatus.STOPPED


@pytest.mark.asyncio
async def test_destroy_sandbox_returns_false_for_unknown():
    """destroy_sandbox should return False for non-existent sandboxes."""
    from orchestrator import sandbox_manager

    result = await sandbox_manager.destroy_sandbox("sbx-ghost")
    assert result is False


@pytest.mark.asyncio
async def test_get_sandbox_returns_none_for_unknown():
    """get_sandbox should return None for non-existent sandboxes."""
    from orchestrator import sandbox_manager

    result = await sandbox_manager.get_sandbox("sbx-nope")
    assert result is None


@pytest.mark.asyncio
async def test_get_sandbox_returns_sandbox(clean_sandbox_state):
    """get_sandbox should return the sandbox when it exists."""
    from orchestrator import sandbox_manager

    store = clean_sandbox_state
    expected = SandboxResponse(
        sandbox_id="sbx-found", user_id="bob", status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc),
    )
    await store.save(expected)

    result = await sandbox_manager.get_sandbox("sbx-found")
    assert result is not None
    assert result.sandbox_id == "sbx-found"
    assert result.user_id == "bob"
