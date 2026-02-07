"""Sandbox lifecycle manager — creates, monitors, and destroys Docker containers.

Manages the full lifecycle: create, wait-for-ready, exec, heartbeat, destroy.
Uses a singleton Docker client to avoid connection leaks.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import docker
from docker.errors import NotFound, APIError

from orchestrator.config import settings
from orchestrator.models import SandboxResponse, SandboxStatus

logger = logging.getLogger(__name__)

# In-memory sandbox registry (replace with DB in production)
_sandboxes: dict[str, SandboxResponse] = {}

# Singleton Docker client — avoids creating new connections on every call (C7)
_docker_client: docker.DockerClient | None = None


def _get_docker_client() -> docker.DockerClient:
    """Get or create the singleton Docker client.

    Returns a cached Docker client instance. The client is reused across all
    operations to avoid leaking TCP connections and file descriptors.

    Raises:
        RuntimeError: If unable to connect to the Docker daemon.
    """
    global _docker_client
    if _docker_client is None:
        try:
            _docker_client = docker.from_env()
        except docker.errors.DockerException as e:
            raise RuntimeError(f"Failed to connect to Docker daemon: {e}") from e
    return _docker_client


def close_docker_client() -> None:
    """Explicitly close the Docker client. Called during app shutdown."""
    global _docker_client
    if _docker_client is not None:
        _docker_client.close()
        _docker_client = None


async def create_sandbox(user_id: str, config: dict | None = None) -> SandboxResponse:
    """Create and start a new sandbox container for a user."""
    sandbox_id = f"sbx-{uuid.uuid4().hex[:12]}"
    config = config or {}

    sandbox = SandboxResponse(
        sandbox_id=sandbox_id,
        user_id=user_id,
        status=SandboxStatus.CREATING,
        created_at=datetime.now(timezone.utc),
    )
    _sandboxes[sandbox_id] = sandbox

    logger.info("Creating sandbox %s for user %s", sandbox_id, user_id)

    try:
        client = _get_docker_client()

        env = {
            "SANDBOX_ID": sandbox_id,
            "USER_ID": user_id,
            "S3_BUCKET": config.get("s3_bucket", settings.s3_bucket),
            "S3_REGION": config.get("s3_region", settings.s3_region),
            "HOT_PATH": "/home/agent",
            "COLD_PATH": "/data/cold",
            "SHUTDOWN_TIMEOUT_SECONDS": str(settings.shutdown_timeout_seconds),
        }

        container = client.containers.run(
            image=settings.sandbox_image,
            name=sandbox_id,
            detach=True,
            environment=env,
            # Resource constraints
            cpu_period=100000,
            cpu_quota=int(settings.container_cpu_limit * 100000),
            mem_limit=settings.container_memory_limit,
            # FUSE requires SYS_ADMIN capability and /dev/fuse access
            cap_add=["SYS_ADMIN"],
            devices=["/dev/fuse"],
            # Security: drop other dangerous capabilities
            cap_drop=["NET_RAW"],
            # Networking
            network=settings.docker_network,
            # Extra hosts so container can reach the orchestrator
            extra_hosts={"host.docker.internal": "host-gateway"},
            labels={
                "matrx.sandbox_id": sandbox_id,
                "matrx.user_id": user_id,
                "matrx.created_at": sandbox.created_at.isoformat(),
            },
            restart_policy={"Name": "no"},
        )

        sandbox.container_id = container.id
        sandbox.status = SandboxStatus.STARTING
        _sandboxes[sandbox_id] = sandbox

        sandbox = await _wait_for_ready(sandbox)
        _sandboxes[sandbox_id] = sandbox

        logger.info("Sandbox %s is %s for user %s", sandbox_id, sandbox.status, user_id)
        return sandbox

    except Exception as e:
        sandbox.status = SandboxStatus.FAILED
        _sandboxes[sandbox_id] = sandbox
        logger.error("Failed to create sandbox %s: %s", sandbox_id, e)
        raise RuntimeError(f"Failed to create sandbox {sandbox_id}: {e}") from e


async def _wait_for_ready(sandbox: SandboxResponse, timeout: int = 120) -> SandboxResponse:
    """Poll container until it signals readiness or times out."""
    client = _get_docker_client()
    elapsed = 0
    interval = 2

    while elapsed < timeout:
        try:
            container = client.containers.get(sandbox.sandbox_id)
            if container.status == "exited":
                sandbox.status = SandboxStatus.FAILED
                return sandbox

            exit_code, _ = container.exec_run("test -f /tmp/.sandbox_ready")
            if exit_code == 0:
                sandbox.status = SandboxStatus.READY
                return sandbox

        except (NotFound, APIError) as e:
            logger.warning("Error polling sandbox %s: %s", sandbox.sandbox_id, e)
            sandbox.status = SandboxStatus.FAILED
            return sandbox

        await asyncio.sleep(interval)
        elapsed += interval

    logger.warning("Sandbox %s did not become ready within %ds", sandbox.sandbox_id, timeout)
    sandbox.status = SandboxStatus.FAILED
    return sandbox


async def get_sandbox(sandbox_id: str) -> SandboxResponse | None:
    """Get sandbox info by ID."""
    return _sandboxes.get(sandbox_id)


async def list_sandboxes(user_id: str | None = None) -> list[SandboxResponse]:
    """List all sandboxes, optionally filtered by user."""
    sandboxes = list(_sandboxes.values())
    if user_id:
        sandboxes = [s for s in sandboxes if s.user_id == user_id]
    return sandboxes


async def exec_in_sandbox(
    sandbox_id: str,
    command: str,
    timeout: int = 30,
    user: str = "agent",
) -> tuple[int, str, str]:
    """Execute a command inside a running sandbox.

    Returns (exit_code, stdout, stderr).

    Validates that the sandbox exists and its container is running before
    executing. Commands are length-limited (C2) and the container status
    is checked (C1/H5) to prevent race conditions.
    """
    sandbox = _sandboxes.get(sandbox_id)
    if not sandbox or not sandbox.container_id:
        raise ValueError(f"Sandbox {sandbox_id} not found or has no container")

    # C2: Validate command length
    if len(command) > settings.max_command_length:
        raise ValueError(
            f"Command exceeds max length ({settings.max_command_length} chars)"
        )

    client = _get_docker_client()
    try:
        container = client.containers.get(sandbox.sandbox_id)

        # C1/H5: Validate container is actually running before exec
        container.reload()
        if container.status != "running":
            raise RuntimeError(
                f"Container for sandbox {sandbox_id} is not running "
                f"(status: {container.status})"
            )

        logger.info(
            "Executing command in sandbox %s (user=%s, timeout=%d, len=%d)",
            sandbox_id, user, timeout, len(command),
        )

        exit_code, output = container.exec_run(
            cmd=["bash", "-c", command],
            user=user,
            demux=True,
        )

        stdout = (output[0] or b"").decode("utf-8", errors="replace")
        stderr = (output[1] or b"").decode("utf-8", errors="replace")
        return exit_code, stdout, stderr

    except (NotFound, APIError) as e:
        raise RuntimeError(f"Failed to exec in sandbox {sandbox_id}: {e}") from e


async def destroy_sandbox(sandbox_id: str, graceful: bool = True) -> bool:
    """Destroy a sandbox, optionally with graceful shutdown.

    After destruction, the sandbox entry is removed from the in-memory
    registry to prevent unbounded memory growth (H6).
    """
    sandbox = _sandboxes.get(sandbox_id)
    if not sandbox:
        return False

    sandbox.status = SandboxStatus.SHUTTING_DOWN
    _sandboxes[sandbox_id] = sandbox

    logger.info("Destroying sandbox %s (graceful=%s)", sandbox_id, graceful)

    client = _get_docker_client()
    try:
        container = client.containers.get(sandbox.sandbox_id)

        if graceful:
            container.stop(timeout=settings.shutdown_timeout_seconds + 10)
        else:
            container.kill()

        container.remove(force=True)
        # H6: Remove from registry to prevent memory leak
        _sandboxes.pop(sandbox_id, None)
        logger.info("Sandbox %s destroyed", sandbox_id)
        return True

    except NotFound:
        _sandboxes.pop(sandbox_id, None)
        logger.warning("Sandbox %s container not found during destroy", sandbox_id)
        return True

    except APIError as e:
        sandbox.status = SandboxStatus.FAILED
        _sandboxes[sandbox_id] = sandbox
        logger.error("Failed to destroy sandbox %s: %s", sandbox_id, e)
        return False


async def heartbeat(sandbox_id: str) -> bool:
    """Record a heartbeat from a sandbox. Returns True if sandbox exists."""
    sandbox = _sandboxes.get(sandbox_id)
    if not sandbox:
        return False
    return True
