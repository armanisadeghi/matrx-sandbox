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
    sandbox_manager._sandbox_cwd.clear()
    yield store
    sandbox_manager._store = None
    sandbox_manager._docker_client = None
    sandbox_manager._sandbox_cwd.clear()


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
async def test_create_sandbox_fetches_vault_with_orchestrator_user_agent(
    mock_docker,
    monkeypatch,
):
    from orchestrator import sandbox_manager
    from orchestrator.config import settings

    container = MagicMock()
    container.id = "abc123"
    container.status = "running"
    container.exec_run.return_value = (0, b"")
    mock_docker.containers.run.return_value = container
    mock_docker.containers.get.return_value = container

    captured_headers: dict[str, str] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"env": {"YOUTUBE_DATA_API_KEY": "test-key"}}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, *, headers, **kwargs):
            captured_headers.update(headers)
            return FakeResponse()

    monkeypatch.setattr(settings, "aidream_url", "https://server.example.com")
    monkeypatch.setattr(settings, "aidream_service_token", "bridge-token")
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)

    sandbox = await sandbox_manager.create_sandbox(user_id="test-user")

    assert captured_headers["User-Agent"] == "matrx-sandbox-orchestrator"
    assert sandbox.config["secrets_injection"]["status_code"] == 200
    assert sandbox.config["secrets_injection"]["fetched_count"] == 1
    run_env = mock_docker.containers.run.call_args.kwargs["environment"]
    assert run_env["YOUTUBE_DATA_API_KEY"] == "test-key"


@pytest.mark.asyncio
async def test_create_sandbox_scopes_vault_fetch_to_organization(mock_docker, monkeypatch):
    """Shared secrets travel only over the service-token hop with org scope."""
    from orchestrator import sandbox_manager
    from orchestrator.config import settings

    container = MagicMock()
    container.id = "org-container"
    container.status = "running"
    container.exec_run.return_value = (0, b"")
    mock_docker.containers.run.return_value = container
    mock_docker.containers.get.return_value = container
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"env": {"ORG_SHARED_KEY": "test-only"}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(settings, "aidream_url", "https://aidream.test")
    monkeypatch.setattr(settings, "aidream_service_token", "service-token")

    organization_id = "884d1ce8-7b49-4fba-a2f3-0f7dd7c83d4f"
    sandbox = await sandbox_manager.create_sandbox(
        user_id="test-user", organization_id=organization_id
    )

    assert captured["params"] == {"organization_id": organization_id}
    assert captured["headers"]["X-Matrx-User-Id"] == "test-user"
    assert sandbox.config["organization_id"] == organization_id
    docker_env = mock_docker.containers.run.call_args.kwargs["environment"]
    assert docker_env["ORGANIZATION_ID"] == organization_id
    assert docker_env["ORG_SHARED_KEY"] == "test-only"


@pytest.mark.asyncio
async def test_aidream_container_rootfs_is_read_only_with_explicit_runtime_tmpfs(
    mock_docker,
):
    container = MagicMock()
    container.id = "abc123"
    container.status = "running"
    container.exec_run.return_value = (0, b"")
    mock_docker.containers.run.return_value = container
    mock_docker.containers.get.return_value = container

    from orchestrator import sandbox_manager

    await sandbox_manager.create_sandbox(
        user_id="00000000-0000-4000-8000-000000000001",
        template="aidream",
        tier="hosted",
    )

    kwargs = mock_docker.containers.run.call_args.kwargs
    assert kwargs["read_only"] is True
    assert kwargs["cap_add"] == []
    assert kwargs["devices"] == []
    assert kwargs["tmpfs"] == {
        "/tmp": "rw,nosuid,nodev,mode=1777",
        "/var/tmp": "rw,nosuid,nodev,mode=1777",
        "/run": "rw,nosuid,nodev,mode=1777",
        "/var/log/sandbox": "rw,nosuid,nodev,mode=0775,uid=1000,gid=1000",
        "/var/log/aidream": "rw,nosuid,nodev,mode=0775,uid=1000,gid=1000",
        "/data/cold": "rw,nosuid,nodev,mode=0775,uid=1000,gid=1000",
    }


def test_runtime_isolation_is_one_shared_policy_for_every_constructor():
    from orchestrator.runtime_isolation import container_runtime_isolation

    aidream = container_runtime_isolation("aidream", "hosted")
    assert aidream["read_only"] is True
    assert "/run" in aidream["tmpfs"]
    assert aidream["cap_add"] == []
    assert aidream["devices"] == []
    assert container_runtime_isolation("aidream", "ec2")["read_only"] is False
    assert container_runtime_isolation("aidream", "ec2")["cap_add"] == ["SYS_ADMIN"]


def test_aidream_warm_pool_is_structurally_prohibited(monkeypatch):
    from orchestrator import pool

    get_client = MagicMock(side_effect=AssertionError("Docker must not be called"))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", get_client)

    assert pool._warm_run_container("aidream") is None
    get_client.assert_not_called()


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


@pytest.mark.asyncio
async def test_exec_in_sandbox_tracks_cwd(mock_docker, clean_sandbox_state):
    """exec_in_sandbox should track CWD across invocations via sentinel."""
    from orchestrator import sandbox_manager

    store = clean_sandbox_state
    await store.save(SandboxResponse(
        sandbox_id="sbx-cwd", user_id="alice", status=SandboxStatus.READY,
        container_id="container-cwd",
        created_at=datetime.now(timezone.utc),
    ))

    container = MagicMock()
    container.status = "running"
    mock_docker.containers.get.return_value = container

    sentinel = sandbox_manager._CWD_SENTINEL

    # Simulate output of 'cd /tmp && { ls ; }; echo SENTINEL; pwd'
    container.exec_run.return_value = (
        0,
        (f"file1.txt\nfile2.txt\n{sentinel}\n/tmp\n".encode(), b""),
    )

    exit_code, stdout, stderr, cwd = await sandbox_manager.exec_in_sandbox(
        "sbx-cwd", "ls"
    )

    assert exit_code == 0
    assert "file1.txt" in stdout
    assert cwd == "/tmp"
    # Sentinel text must NOT leak into user-visible stdout
    assert sentinel not in stdout
    # Server should cache the new CWD
    assert sandbox_manager._sandbox_cwd["sbx-cwd"] == "/tmp"
