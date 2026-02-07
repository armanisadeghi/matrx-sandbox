"""Sandbox lifecycle manager — creates, monitors, and destroys Docker containers."""

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


def _get_docker_client() -> docker.DockerClient:
    return docker.from_env()


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

    try:
        client = _get_docker_client()

        # Build environment variables for the container
        env = {
            "SANDBOX_ID": sandbox_id,
            "USER_ID": user_id,
            "S3_BUCKET": config.get("s3_bucket", settings.s3_bucket),
            "S3_REGION": config.get("s3_region", settings.s3_region),
            "HOT_PATH": "/home/agent",
            "COLD_PATH": "/data/cold",
            "SHUTDOWN_TIMEOUT_SECONDS": str(settings.shutdown_timeout_seconds),
        }

        # Container resource limits
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
            # Labels for management
            labels={
                "matrx.sandbox_id": sandbox_id,
                "matrx.user_id": user_id,
                "matrx.created_at": sandbox.created_at.isoformat(),
            },
            # Restart policy: never (ephemeral)
            restart_policy={"Name": "no"},
        )

        sandbox.container_id = container.id
        sandbox.status = SandboxStatus.STARTING
        _sandboxes[sandbox_id] = sandbox

        # Wait for the container to signal readiness (poll for /tmp/.sandbox_ready)
        sandbox = await _wait_for_ready(sandbox)
        _sandboxes[sandbox_id] = sandbox

        logger.info(f"Sandbox {sandbox_id} created for user {user_id}")
        return sandbox

    except Exception as e:
        sandbox.status = SandboxStatus.FAILED
        _sandboxes[sandbox_id] = sandbox
        logger.error(f"Failed to create sandbox {sandbox_id}: {e}")
        raise


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

            # Check if ready file exists
            exit_code, _ = container.exec_run("test -f /tmp/.sandbox_ready")
            if exit_code == 0:
                sandbox.status = SandboxStatus.READY
                return sandbox

        except (NotFound, APIError) as e:
            logger.warning(f"Error polling sandbox {sandbox.sandbox_id}: {e}")
            sandbox.status = SandboxStatus.FAILED
            return sandbox

        await asyncio.sleep(interval)
        elapsed += interval

    logger.warning(f"Sandbox {sandbox.sandbox_id} did not become ready within {timeout}s")
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


async def exec_in_sandbox(sandbox_id: str, command: str, timeout: int = 30, user: str = "agent") -> tuple[int, str, str]:
    """Execute a command inside a running sandbox. Returns (exit_code, stdout, stderr)."""
    sandbox = _sandboxes.get(sandbox_id)
    if not sandbox or not sandbox.container_id:
        raise ValueError(f"Sandbox {sandbox_id} not found or has no container")

    client = _get_docker_client()
    try:
        container = client.containers.get(sandbox.sandbox_id)
        exit_code, output = container.exec_run(
            cmd=["bash", "-c", command],
            user=user,
            demux=True,  # separate stdout/stderr
        )

        stdout = (output[0] or b"").decode("utf-8", errors="replace")
        stderr = (output[1] or b"").decode("utf-8", errors="replace")
        return exit_code, stdout, stderr

    except (NotFound, APIError) as e:
        raise RuntimeError(f"Failed to exec in sandbox {sandbox_id}: {e}")


async def destroy_sandbox(sandbox_id: str, graceful: bool = True) -> bool:
    """Destroy a sandbox, optionally with graceful shutdown."""
    sandbox = _sandboxes.get(sandbox_id)
    if not sandbox:
        return False

    sandbox.status = SandboxStatus.SHUTTING_DOWN
    _sandboxes[sandbox_id] = sandbox

    client = _get_docker_client()
    try:
        container = client.containers.get(sandbox.sandbox_id)

        if graceful:
            # Send SIGTERM, which triggers the shutdown trap in entrypoint.sh
            container.stop(timeout=settings.shutdown_timeout_seconds + 10)
        else:
            container.kill()

        container.remove(force=True)
        sandbox.status = SandboxStatus.STOPPED
        _sandboxes[sandbox_id] = sandbox
        logger.info(f"Sandbox {sandbox_id} destroyed (graceful={graceful})")
        return True

    except NotFound:
        sandbox.status = SandboxStatus.STOPPED
        _sandboxes[sandbox_id] = sandbox
        logger.warning(f"Sandbox {sandbox_id} container not found during destroy")
        return True

    except APIError as e:
        sandbox.status = SandboxStatus.FAILED
        _sandboxes[sandbox_id] = sandbox
        logger.error(f"Failed to destroy sandbox {sandbox_id}: {e}")
        return False


async def heartbeat(sandbox_id: str) -> bool:
    """Record a heartbeat from a sandbox. Returns True if sandbox exists."""
    sandbox = _sandboxes.get(sandbox_id)
    if not sandbox:
        return False
    # In production, update last_heartbeat timestamp
    return True
