"""Sandbox lifecycle manager — creates, monitors, and destroys Docker containers.

Manages the full lifecycle: create, wait-for-ready, exec, heartbeat, destroy.
Uses a singleton Docker client to avoid connection leaks.
Uses a pluggable SandboxStore for persistence (in-memory or Postgres).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import uuid
from datetime import datetime, timezone

import docker
from docker.errors import DockerException, NotFound, APIError

from orchestrator.config import settings
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.storage_layout import (
    StorageLocation,
    ensure_user_volume,
    resolve_user_storage,
)
from orchestrator.store import SandboxStore, create_store

logger = logging.getLogger(__name__)

# ── CWD tracking ──────────────────────────────────────────────────────────────
# Each sandbox has a server-side CWD so stateless Docker exec calls can chain
# directory changes across requests (e.g. "cd home" then "ls" shows /home).
_CWD_SENTINEL = "___MATRX_CWD_SENTINEL_9f8a7b___"
_DEFAULT_CWD = "/home/agent"
_sandbox_cwd: dict[str, str] = {}  # sandbox_id -> current working directory

# Pluggable sandbox store (replaces old in-memory _sandboxes dict)
_store: SandboxStore | None = None

# Singleton Docker client — avoids creating new connections on every call (C7)
_docker_client: docker.DockerClient | None = None


def _resolve_passthrough_keys() -> list[str]:
    """Union of names from settings.aidream_passthrough_env_file (parsed once,
    cached) and settings.aidream_passthrough_env (comma-separated). Values
    are read from os.environ at call time — this only resolves NAMES.

    Reading the file fresh each call would be wasted I/O; cache by mtime so
    if the operator updates aidream's .env and restarts, we pick it up.
    """
    keys: set[str] = set()
    explicit = (settings.aidream_passthrough_env or "").split(",")
    keys.update(k.strip() for k in explicit if k.strip())

    path = (settings.aidream_passthrough_env_file or "").strip()
    if path and os.path.isfile(path):
        keys.update(_parse_env_file_keys(path))
    return sorted(keys)


_env_file_cache: dict[str, tuple[float, frozenset[str]]] = {}


def _parse_env_file_keys(path: str) -> frozenset[str]:
    """Return the set of env-var NAMES defined in a .env-style file. Cached
    by (path, mtime) so repeated calls don't re-read the file."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return frozenset()
    cached = _env_file_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    keys: set[str] = set()
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Accept "FOO=bar", "export FOO=bar", "FOO =". Reject lines
                # without "=" (they aren't env-vars).
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, sep, _ = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                # Defensive: skip lines that aren't valid env-var names so
                # garbage in the file can't poison the passthrough set.
                if key and all(c.isalnum() or c == "_" for c in key) and not key[0].isdigit():
                    keys.add(key)
    except OSError as exc:
        logger.warning("could not read aidream_passthrough_env_file %s: %s", path, exc)
        return frozenset()

    frozen = frozenset(keys)
    _env_file_cache[path] = (mtime, frozen)
    logger.info("loaded %d passthrough keys from %s", len(frozen), path)
    return frozen


def _proxy_url_for(sandbox_id: str) -> str | None:
    """Build the public ``proxy_url`` field surfaced on SandboxResponse.

    The browser hits ``{proxy_url}/<path>`` (e.g. ``/ai/agents/.../execute``)
    and the orchestrator forwards 1:1 to the in-container daemon. Returns
    None when MATRX_PUBLIC_URL is unset (the orchestrator doesn't know how
    it is reachable from the outside, so it can't build a URL).
    """
    base = (settings.public_url or "").rstrip("/")
    if not base:
        return None
    return f"{base}/sandboxes/{sandbox_id}/proxy"


def _resolve_ssh_host() -> str:
    """Public SSH host returned to clients in /access responses.

    Priority:
      1. Explicit ``MATRX_SSH_HOST`` env var (overrides; supports the case
         where SSH and HTTP exit through different hostnames).
      2. Hostname parsed out of ``MATRX_PUBLIC_URL``.
      3. Fallback to ``"localhost"`` — only useful for in-host testing;
         logs a loud warning when this fallback is hit.
    """
    if settings.ssh_host:
        return settings.ssh_host
    if settings.public_url:
        from urllib.parse import urlparse
        parsed = urlparse(settings.public_url)
        if parsed.hostname:
            return parsed.hostname
    logger.warning(
        "SSH host falling back to 'localhost' — set MATRX_SSH_HOST or MATRX_PUBLIC_URL "
        "so /sandboxes/{id}/access returns a hostname clients can actually reach."
    )
    return "localhost"


def _get_store() -> SandboxStore:
    """Get or create the sandbox store."""
    global _store
    if _store is None:
        _store = create_store()
    return _store


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
        except DockerException as e:
            raise RuntimeError(f"Failed to connect to Docker daemon: {e}") from e
    return _docker_client


def close_docker_client() -> None:
    """Explicitly close the Docker client. Called during app shutdown."""
    global _docker_client
    if _docker_client is not None:
        _docker_client.close()
        _docker_client = None


async def close_store() -> None:
    """Close the sandbox store. Called during app shutdown."""
    global _store
    if _store is not None:
        await _store.close()
        _store = None


async def create_sandbox(
    user_id: str,
    config: dict | None = None,
    template: str | None = None,
    template_version: str | None = None,
    tier: str | None = None,
    resources: dict | None = None,
    labels: dict | None = None,
    ttl_seconds: int | None = None,
) -> SandboxResponse:
    """Create and start a new sandbox container for a user.

    ``template``/``template_version``/``tier``/``labels`` are recorded on the
    sandbox row for routing + observability. ``resources`` overrides the per-
    container CPU/memory limits when supplied (hosted tier only — EC2 tier ignores
    overrides for now). ``ttl_seconds`` overrides the default TTL for this sandbox.
    """
    store = _get_store()
    sandbox_id = f"sbx-{uuid.uuid4().hex[:12]}"
    config = config or {}
    resources = resources or {}

    sandbox = SandboxResponse(
        sandbox_id=sandbox_id,
        user_id=user_id,
        status=SandboxStatus.CREATING,
        created_at=datetime.now(timezone.utc),
        config=config,
        ttl_seconds=ttl_seconds or 7200,
        tier=tier,
        template=template,
        template_version=template_version,
        labels=labels,
        proxy_url=_proxy_url_for(sandbox_id),
    )
    await store.save(sandbox)

    logger.info(
        "Creating sandbox %s for user %s (tier=%s, template=%s)",
        sandbox_id, user_id, tier, template,
    )

    try:
        client = _get_docker_client()

        # ── Resolve persistence location for this (user, tier) pair ───────────
        location: StorageLocation = resolve_user_storage(user_id, tier)
        volumes: dict[str, dict] = {}
        if location.tier == "hosted":
            # Hosted tier: per-user Docker volume mounted at /home/agent.
            # Volume survives container destruction; subsequent sandboxes for
            # the same user see the same home dir.
            volume_name = ensure_user_volume(client, user_id)
            volumes[volume_name] = {"bind": "/home/agent", "mode": "rw"}
            sandbox.persistence_volume = volume_name
            logger.info(
                "Hosted-tier sandbox %s: mounting user volume %s -> /home/agent",
                sandbox_id, volume_name,
            )

        env = {
            "SANDBOX_ID": sandbox_id,
            "USER_ID": user_id,
            "S3_BUCKET": location.s3_bucket or "",
            "S3_REGION": config.get("s3_region", settings.s3_region),
            "HOT_PATH": "/home/agent",
            "COLD_PATH": "/data/cold",
            "SHUTDOWN_TIMEOUT_SECONDS": str(settings.shutdown_timeout_seconds),
            # Tier hint — the in-container persistence module reads this to
            # decide whether to also push to S3 (Phase 1.5). When empty / "ec2"
            # the existing hot-sync.sh + cold-mount.sh handle S3 directly.
            "MATRX_TIER": location.tier,
            "MATRX_HOT_PREFIX": location.s3_hot_prefix or "",
            "MATRX_COLD_PREFIX": location.s3_cold_prefix or "",
        }
        if template:
            env["SANDBOX_TEMPLATE"] = template
        if template_version:
            env["SANDBOX_TEMPLATE_VERSION"] = template_version

        # ── AI Dream integration env ──────────────────────────────────────────
        # When the orchestrator has a service token, hand it to the sandbox
        # along with the user_id. The in-container ``mtx`` CLI uses this to
        # call AI Dream's cld_files endpoints on behalf of the user.
        if settings.aidream_url:
            env["MATRX_AIDREAM_URL"] = settings.aidream_url
        if settings.aidream_service_token:
            env["MATRX_AIDREAM_SERVICE_TOKEN"] = settings.aidream_service_token

        # ── AWS creds for hosted-tier S3 sync ────────────────────────────────
        # On EC2 tier the orchestrator has an instance role and skips this
        # block — sandboxes inherit creds via the role. On hosted tier we
        # have to pass explicit creds (orchestrator reads them from its own
        # env via ``settings.aws_access_key_id`` / ``aws_secret_access_key``).
        if location.tier == "hosted" and settings.aws_access_key_id and settings.aws_secret_access_key:
            env["AWS_ACCESS_KEY_ID"] = settings.aws_access_key_id
            env["AWS_SECRET_ACCESS_KEY"] = settings.aws_secret_access_key
            # Pass BOTH region env-var conventions. boto3 honors either, but
            # downstream code may explicitly check just one — aidream's
            # common/aws/aws_client.py reads AWS_REGION specifically.
            region = config.get("s3_region", settings.s3_region)
            env["AWS_DEFAULT_REGION"] = region
            env["AWS_REGION"] = region

        # ── aidream-in-sandbox env passthrough ────────────────────────────────
        # Forward every env var named in EITHER (a) the file at
        # settings.aidream_passthrough_env_file (e.g. /srv/projects/aidream/.env)
        # or (b) the explicit settings.aidream_passthrough_env list, from the
        # orchestrator's process environment into the spawned container.
        # Values not present in the orchestrator's environ are silently
        # skipped (list a superset safely). The file-based mechanism keeps
        # this auto-synced as aidream adds new required env vars.
        passthrough_keys = _resolve_passthrough_keys()
        for key in passthrough_keys:
            val = os.environ.get(key)
            if val and key not in env:  # don't clobber already-set vars
                env[key] = val

        # ── Path-shape overrides ──────────────────────────────────────────────
        # /srv/projects/aidream/.env was authored on the maintainer's Mac and
        # ships paths like BASE_DIR=/Users/armanisadeghi/code/aidream. Those
        # paths don't exist inside the sandbox (aidream is at /home/agent/aidream).
        # Override the path-shaped vars so matrx-utils settings + file-handling
        # code resolves real on-disk locations. Anything else (API keys,
        # secrets, Supabase connection bits) flows through verbatim.
        if (template or "") == "aidream":
            sandbox_home = "/home/agent"
            sandbox_aidream_root = f"{sandbox_home}/aidream"
            env["BASE_DIR"] = sandbox_aidream_root
            env["TEMP_DIR"] = f"{sandbox_aidream_root}/temp"
            env["LOG_DIR"] = f"{sandbox_aidream_root}/temp/logs"
            env["MEDIA_BASE_DIR"] = f"{sandbox_aidream_root}/temp/media"
            env["SAMPLE_DATA_DIR"] = f"{sandbox_aidream_root}/common/sample_data"
            # matrx-ai filesystem tools (fs_list, fs_read, fs_search, ...) gate
            # every path against TOOL_WORKSPACE_BASE. Aidream's .env points it
            # at the maintainer's local /Users/.../temp, which strands every
            # tool call with "Directory not found" / "Path escapes workspace".
            # Inside the sandbox the natural workspace IS the agent's home —
            # the container itself is the user/project boundary.
            env["TOOL_WORKSPACE_BASE"] = sandbox_home
            # The sniff-marker (/opt/aidream-template) is baked into the image;
            # set the explicit env signal too so tools never fall back to the
            # multi-tenant /tmp/workspaces layout even if the marker is moved.
            env["MATRX_TOOLS_SANDBOX_MODE"] = "1"
            # Other Mac-shaped paths that aidream code reads at runtime.
            env["ADMIN_PYTHON_ROOT"] = sandbox_aidream_root
            env["ADMIN_TS_ROOT"] = sandbox_aidream_root
            env["ADMIN_SAVE_DIRECT_ROOT"] = sandbox_aidream_root
            env["MATRX_PYTHON_ROOT"] = sandbox_aidream_root
            env["PYTHONPATH"] = sandbox_aidream_root
            # GOOGLE_APPLICATION_CREDENTIALS points at a Mac filesystem path
            # that doesn't exist here — having it set causes google-auth to
            # try to open a non-existent file. Drop it so the Google SDKs
            # gracefully fall back to other auth signals (or stay unconfigured).
            env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

        # Resource overrides — fall back to settings defaults
        cpu_limit = resources.get("cpu") or settings.container_cpu_limit
        memory_limit = resources.get("memory_mb")
        memory_limit = f"{memory_limit}m" if memory_limit else settings.container_memory_limit

        # Per-template image override. Most templates share the bare image
        # (they differ via env vars / init scripts), but variants like
        # 'aidream' need a heavier purpose-built image.
        from orchestrator.routes.templates import resolve_template_image
        image_for_template = resolve_template_image(template) or settings.sandbox_image

        container = client.containers.run(
            image=image_for_template,
            name=sandbox_id,
            detach=True,
            environment=env,
            volumes=volumes or None,
            # Resource constraints
            cpu_period=100000,
            cpu_quota=int(cpu_limit * 100000),
            mem_limit=memory_limit,
            # FUSE requires SYS_ADMIN capability and /dev/fuse access
            cap_add=["SYS_ADMIN"],
            devices=["/dev/fuse"],
            # No capability drops — the container IS the security boundary
            cap_drop=[],
            # Expose SSH (port 22) on a dynamic host port
            ports={"22/tcp": None},
            # Networking
            network=settings.docker_network,
            # Extra hosts so container can reach the orchestrator
            extra_hosts={"host.docker.internal": "host-gateway"},
            labels={
                "matrx.sandbox_id": sandbox_id,
                "matrx.user_id": user_id,
                "matrx.created_at": sandbox.created_at.isoformat(),
                # Labels carry the *minimum* state needed for boot-time
                # reconcile to rebuild a SandboxResponse without help from
                # the store. If you add a new field to SandboxResponse that
                # the agent or FE depends on, add it here too.
                **({"matrx.tier": tier} if tier else {}),
                **({"matrx.template": template} if template else {}),
                **({"matrx.template_version": template_version} if template_version else {}),
                **{f"matrx.label.{k}": v for k, v in (labels or {}).items()},
            },
            restart_policy={"Name": "no", "MaximumRetryCount": 0},
        )  # type: ignore[call-overload]

        sandbox.container_id = container.id
        sandbox.status = SandboxStatus.STARTING

        # Read back the dynamically assigned SSH host port
        container.reload()
        port_bindings = container.attrs["NetworkSettings"]["Ports"].get("22/tcp")
        if port_bindings:
            sandbox.ssh_port = int(port_bindings[0]["HostPort"])

        await store.save(sandbox)

        sandbox = await _wait_for_ready(sandbox)
        await store.save(sandbox)

        logger.info("Sandbox %s is %s for user %s", sandbox_id, sandbox.status, user_id)
        return sandbox

    except Exception as e:
        sandbox.status = SandboxStatus.FAILED
        await store.save(sandbox)
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


def _backfill_proxy_url(sb: SandboxResponse | None) -> SandboxResponse | None:
    """Stamp proxy_url on a SandboxResponse loaded from storage.

    The store may hold rows persisted before MATRX_PUBLIC_URL was configured,
    or before the proxy_url field existed; recompute on read so clients
    always see the current orchestrator's idea of the URL.
    """
    if sb is not None and not sb.proxy_url:
        sb.proxy_url = _proxy_url_for(sb.sandbox_id)
    return sb


async def get_sandbox(sandbox_id: str) -> SandboxResponse | None:
    """Get sandbox info by ID."""
    store = _get_store()
    return _backfill_proxy_url(await store.get(sandbox_id))


async def list_sandboxes(user_id: str | None = None) -> list[SandboxResponse]:
    """List all sandboxes, optionally filtered by user."""
    store = _get_store()
    rows = await store.list(user_id=user_id)
    for sb in rows:
        _backfill_proxy_url(sb)
    return rows


def get_sandbox_internal_ip(sandbox_id: str) -> str | None:
    """Get the internal Docker IP address of a running sandbox."""
    client = _get_docker_client()
    try:
        container = client.containers.get(sandbox_id)
        return container.attrs.get("NetworkSettings", {}).get("Networks", {}).get(settings.docker_network, {}).get("IPAddress")
    except (NotFound, APIError):
        return None



async def exec_in_sandbox(
    sandbox_id: str,
    command: str,
    timeout: int = 30,
    user: str = "agent",
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> tuple[int, str, str, str]:
    """Execute a command inside a running sandbox with CWD tracking.

    Returns (exit_code, stdout, stderr, cwd_after).

    The command is wrapped so it runs in the tracked working directory.
    After execution, the new CWD is captured via a sentinel/pwd trailer
    and cached for the next call.  Clients may also pass an explicit
    ``cwd`` to override the server-tracked value.

    ``env`` is merged into the container default env for this single exec.
    ``stdin`` is fed to the command on stdin; large stdin payloads bypass the
    command-length cap (use this instead of inline heredocs).
    """
    store = _get_store()
    sandbox = await store.get(sandbox_id)
    if not sandbox or not sandbox.container_id:
        raise ValueError(f"Sandbox {sandbox_id} not found or has no container")

    # C2: Validate command length
    if len(command) > settings.max_command_length:
        raise ValueError(
            f"Command exceeds max length ({settings.max_command_length} chars)"
        )

    # Resolve CWD: explicit param > server cache > default
    effective_cwd = cwd or _sandbox_cwd.get(sandbox_id, _DEFAULT_CWD)

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
            "Executing command in sandbox %s (user=%s, timeout=%d, len=%d, cwd=%s, env_keys=%d, stdin=%s)",
            sandbox_id, user, timeout, len(command), effective_cwd,
            len(env or {}), "yes" if stdin else "no",
        )

        # Wrap the command so it:
        #   1. cd's into the tracked CWD
        #   2. runs the user command
        #   3. prints a sentinel + pwd so we can capture the new CWD
        wrapped = (
            f"cd {shlex.quote(effective_cwd)} && "
            f"{{ {command} ; }}; "
            f"__matrx_ec=$?; "
            f"echo '{_CWD_SENTINEL}'; "
            f"pwd; "
            f"exit $__matrx_ec"
        )

        exec_kwargs: dict = dict(
            cmd=["bash", "-c", wrapped],
            user=user,
            demux=True,
        )
        if env:
            exec_kwargs["environment"] = env
        if stdin is not None:
            # docker-py exec_run with stdin requires socket attach; we use create+start.
            return await _exec_with_stdin(
                container, wrapped, user, env or {}, stdin, sandbox_id, effective_cwd,
            )

        exit_code, output = container.exec_run(**exec_kwargs)

        stdout_raw = (output[0] or b"").decode("utf-8", errors="replace")
        stderr = (output[1] or b"").decode("utf-8", errors="replace")

        # Parse sentinel to extract real stdout and new CWD
        stdout, new_cwd = _parse_cwd_sentinel(stdout_raw)
        if new_cwd:
            _sandbox_cwd[sandbox_id] = new_cwd
        else:
            new_cwd = effective_cwd

        return exit_code, stdout, stderr, new_cwd

    except (NotFound, APIError) as e:
        raise RuntimeError(f"Failed to exec in sandbox {sandbox_id}: {e}") from e


async def _exec_with_stdin(
    container,
    wrapped: str,
    user: str,
    env: dict[str, str],
    stdin_data: str,
    sandbox_id: str,
    effective_cwd: str,
) -> tuple[int, str, str, str]:
    """Run an exec where stdin is fed to the command.

    docker-py's ``exec_run`` doesn't accept a stdin payload directly. We use
    the lower-level ``exec_create`` + ``exec_start`` pair with ``socket=True``
    to write the input, then collect output.
    """
    api = container.client.api
    exec_id = api.exec_create(
        container.id,
        cmd=["bash", "-c", wrapped],
        user=user,
        environment=env,
        stdin=True,
        tty=False,
        stdout=True,
        stderr=True,
    )["Id"]

    sock = api.exec_start(exec_id, socket=True, demux=True)
    raw = sock._sock if hasattr(sock, "_sock") else sock
    try:
        raw.sendall(stdin_data.encode("utf-8"))
        try:
            raw.shutdown(1)  # SHUT_WR — signal EOF on stdin
        except OSError:
            pass

        chunks_out = bytearray()
        chunks_err = bytearray()
        while True:
            data = raw.recv(65536)
            if not data:
                break
            chunks_out.extend(data)
        # NOTE: exec_start with demux=True returns multiplexed frames; we
        # accept the simpler interleaved approach for stdin-fed execs and
        # surface the combined stream as stdout. The frontend's primary use
        # case for stdin is non-interactive piping (heredoc replacement).
    finally:
        try:
            raw.close()
        except OSError:
            pass

    inspect = api.exec_inspect(exec_id)
    exit_code = inspect.get("ExitCode") or 0
    stdout_raw = chunks_out.decode("utf-8", errors="replace")
    stderr = chunks_err.decode("utf-8", errors="replace")

    stdout, new_cwd = _parse_cwd_sentinel(stdout_raw)
    if new_cwd:
        _sandbox_cwd[sandbox_id] = new_cwd
    else:
        new_cwd = effective_cwd
    return exit_code, stdout, stderr, new_cwd


def _parse_cwd_sentinel(raw: str) -> tuple[str, str | None]:
    """Split stdout at the CWD sentinel, returning (visible_output, new_cwd)."""
    idx = raw.rfind(_CWD_SENTINEL)
    if idx == -1:
        return raw, None
    output = raw[:idx].rstrip("\n")
    after = raw[idx + len(_CWD_SENTINEL):].strip()
    lines = after.split("\n")
    new_cwd = lines[0].strip() if lines else None
    return output, new_cwd


def forget_sandbox_cwd(sandbox_id: str) -> None:
    """Remove CWD state for a destroyed sandbox."""
    _sandbox_cwd.pop(sandbox_id, None)


async def destroy_sandbox(
    sandbox_id: str,
    graceful: bool = True,
    reason: str = "user_requested",
) -> bool:
    """Destroy a sandbox, optionally with graceful shutdown.

    Records the stop reason (user_requested, expired, error, graceful_shutdown, admin)
    and marks the sandbox as stopped rather than deleting, preserving audit history.
    """
    store = _get_store()
    sandbox = await store.get(sandbox_id)
    if not sandbox:
        return False

    forget_sandbox_cwd(sandbox_id)

    await store.update_status(sandbox_id, SandboxStatus.SHUTTING_DOWN)

    logger.info("Destroying sandbox %s (graceful=%s, reason=%s)", sandbox_id, graceful, reason)

    client = _get_docker_client()
    try:
        container = client.containers.get(sandbox.sandbox_id)

        if graceful:
            container.stop(timeout=settings.shutdown_timeout_seconds + 10)
        else:
            container.kill()

        # ``container.remove`` deletes the container itself — anonymous
        # volumes go with it, but the *named* per-user volume we mounted
        # at /home/agent (hosted tier) is preserved. That's the whole
        # point of the persistence model: user data survives sandbox
        # lifecycle. Explicit volume wipe is a separate, admin-only path
        # (see ``delete_user_volume``).
        container.remove(force=True)

        await store.mark_stopped(sandbox_id, reason)

        logger.info(
            "Sandbox %s destroyed (volume %s preserved)",
            sandbox_id, sandbox.persistence_volume or "n/a — EC2 tier",
        )
        return True

    except NotFound:
        await store.mark_stopped(sandbox_id, reason)
        logger.warning("Sandbox %s container not found during destroy", sandbox_id)
        return True

    except APIError as e:
        await store.update_status(sandbox_id, SandboxStatus.FAILED)
        logger.error("Failed to destroy sandbox %s: %s", sandbox_id, e)
        return False


async def delete_user_volume(user_id: str) -> bool:
    """Hard-delete a user's per-user Docker volume (hosted tier only).

    Use case: a user explicitly clicks "Delete my persistent storage" in the
    admin panel, OR an admin needs to forcibly wipe a user's home dir. This is
    a destructive operation — there's no undo. Refuses to run if the volume
    has active containers attached.
    """
    from orchestrator.storage_layout import user_volume_name

    name = user_volume_name(user_id)
    client = _get_docker_client()

    # Refuse if any sandbox is still using the volume — would surprise the user.
    in_use = client.containers.list(
        all=True,
        filters={"volume": name},
    )
    if in_use:
        raise RuntimeError(
            f"Volume {name} is still in use by {len(in_use)} container(s). "
            "Stop those sandboxes first."
        )

    try:
        volume = client.volumes.get(name)
    except NotFound:
        logger.info("delete_user_volume(%s): volume not found, no-op", user_id)
        return True

    try:
        volume.remove(force=False)
        logger.warning("Deleted user volume %s for user %s", name, user_id)
        return True
    except APIError as e:
        logger.error("Failed to delete volume %s: %s", name, e)
        return False


def get_user_volume_size(user_id: str) -> int | None:
    """Return the current size in bytes of a user's hosted-tier volume.

    None if the volume doesn't exist (user has no hosted-tier data yet) or
    Docker doesn't expose UsageData (older daemons / flag-disabled setups).
    Cheap call — uses the docker daemon's own metadata, doesn't `du`.
    """
    from orchestrator.storage_layout import user_volume_name

    name = user_volume_name(user_id)
    client = _get_docker_client()
    try:
        # API endpoint /volumes returns a list with UsageData when invoked
        # with the `?status=true` flag-equivalent in the SDK.
        volumes_data = client.api.volumes()
        for v in volumes_data.get("Volumes") or []:
            if v.get("Name") == name:
                usage = v.get("UsageData") or {}
                size = usage.get("Size")
                # Docker returns -1 when usage data isn't enabled (default).
                # We need a separate `du -sb` path for accurate sizes; punt
                # to Phase 5 (quotas) where that lives.
                return size if isinstance(size, int) and size >= 0 else None
    except APIError as e:
        logger.warning("Failed to query volume size for %s: %s", name, e)
        return None
    return None


async def heartbeat(sandbox_id: str) -> bool:
    """Record a heartbeat from a sandbox. Returns True if sandbox exists."""
    store = _get_store()
    sandbox = await store.get(sandbox_id)
    if not sandbox:
        return False
    await store.update_heartbeat(sandbox_id)
    return True


async def generate_user_access(sandbox_id: str) -> dict[str, str | int]:
    """Generate a temporary SSH keypair and inject the public key into the sandbox.

    Returns a dict with private_key (PEM), username, host, and port.
    The private key is generated per-request and never stored by the orchestrator.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    store = _get_store()
    sandbox = await store.get(sandbox_id)
    if not sandbox:
        raise ValueError(f"Sandbox {sandbox_id} not found")

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode()

    inject_cmd = (
        "mkdir -p /home/agent/.ssh && "
        f"echo '{public_key} user-access' >> /home/agent/.ssh/authorized_keys && "
        "chown agent:agent /home/agent/.ssh/authorized_keys && "
        "chmod 600 /home/agent/.ssh/authorized_keys"
    )
    await exec_in_sandbox(sandbox_id, inject_cmd, user="root")

    logger.info("Generated temporary SSH access for sandbox %s", sandbox_id)

    return {
        "private_key": private_pem,
        "username": "agent",
        "host": _resolve_ssh_host(),
        "port": sandbox.ssh_port or 22,
    }
