"""Sandbox CRUD API routes."""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
import httpx

from orchestrator import activity, sandbox_manager, storage
from orchestrator.auth import sandbox_token
from orchestrator.config import settings
from orchestrator.models import (
    AccessResponse,
    AccessTokenRequest,
    AgentBindingRequest,
    AccessTokenResponse,
    CompletionRequest,
    CompletionResponse,
    CreateSandboxRequest,
    ErrorReport,
    ErrorResponse,
    ExecRequest,
    ExecResponse,
    ExtendRequest,
    ExtendResponse,
    HeartbeatResponse,
    SandboxListResponse,
    SandboxResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


def _with_agent(headers: dict, sandbox_id: str) -> dict:
    """Add the per-sandbox daemon token to forwarded headers so the in-container
    daemon accepts the proxied request. No-op when daemon enforcement is off
    (no access-token secret configured). Always set by us — a client-supplied
    value must not override the orchestrator's."""
    headers.update(sandbox_manager.agent_forward_headers(sandbox_id))
    return headers


@router.post("", response_model=SandboxResponse, status_code=201)
async def create_sandbox(req: CreateSandboxRequest):
    """Create a new sandbox for a user.

    The ``tier`` field is advisory: each orchestrator only spawns sandboxes for
    its own tier (set via ``SANDBOX_HOST_TIER``). If a request specifies a tier
    that doesn't match this orchestrator's tier, it is rejected with 400 so the
    frontend can route to the correct orchestrator.
    """
    logger.info(
        "Sandbox creation requested for user_id=%s (tier=%s, template=%s)",
        req.user_id, req.tier, req.template,
    )

    if req.tier and settings.host_tier and req.tier != settings.host_tier:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Tier mismatch: this orchestrator hosts tier '{settings.host_tier}', "
                f"but the request asked for '{req.tier}'. Route the request to the "
                "appropriate orchestrator URL."
            ),
        )

    effective_tier = req.tier or settings.host_tier

    # The aidream template is a 5 GB image baked + venv-resolved only on the
    # hosted tier (this server). EC2's ECR repo doesn't carry it, so an
    # EC2 + aidream create silently fails with container_id=null. Reject it
    # up front with a message the FE can show, instead of leaving a "failed"
    # row the user has to puzzle over.
    if (req.template == "aidream") and effective_tier == "ec2":
        raise HTTPException(
            status_code=400,
            detail=(
                "The 'aidream' template is only available on the hosted tier. "
                "EC2-tier sandboxes don't carry the aidream image. Either pick "
                "the hosted tier, or choose a non-aidream template for EC2."
            ),
        )

    await storage.ensure_user_storage(req.user_id)
    sandbox = await sandbox_manager.create_sandbox(
        user_id=req.user_id,
        config=req.config,
        template=req.template,
        template_version=req.template_version,
        tier=effective_tier,
        resources=req.resources.model_dump(exclude_none=True) if req.resources else None,
        labels=req.labels,
        ttl_seconds=req.ttl_seconds,
    )
    return sandbox


@router.post("/claim", response_model=SandboxResponse, status_code=201)
async def claim_sandbox(req: CreateSandboxRequest):
    """Launch fast by CLAIMing a pre-warmed box; fall back to a cold create.

    Same request shape as ``POST /sandboxes``. If a warm box of the requested
    template is available it's adopted (the user's memory is hydrated into it)
    and returned in seconds; the pool replenishes in the background. If the
    pool is empty / disabled, this transparently cold-creates so callers can
    always use ``/claim`` and just get the fast path when it's available.
    """
    if req.tier and settings.host_tier and req.tier != settings.host_tier:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Tier mismatch: this orchestrator hosts tier '{settings.host_tier}', "
                f"but the request asked for '{req.tier}'."
            ),
        )

    from orchestrator import pool
    template = req.template or settings.warm_pool_template

    # Warm-pool sandboxes are pre-booted with NO per-user env, and Docker
    # cannot change the environment of a running container — so a /claim
    # against a warm box would silently drop any `config.env` the caller
    # supplied (user secrets from aidream's vault, sandbox-prefs env).
    # When the caller has secrets to inject, skip the warm fast path and
    # cold-create with the full env. This costs the warm-pool latency
    # benefit but keeps the contract honest: every secret a user has set
    # is in the container env from boot.
    config_env = (req.config or {}).get("env") if isinstance(req.config, dict) else None
    has_inject_env = isinstance(config_env, dict) and len(config_env) > 0
    if has_inject_env:
        logger.info(
            "Skipping warm pool for user %s — config.env has %d keys "
            "to inject (warm boxes can't accept new env post-boot)",
            req.user_id, len(config_env),
        )
        return await create_sandbox(req)

    claimed = await pool.claim_warm(
        user_id=req.user_id,
        template=template,
        ttl_seconds=req.ttl_seconds,
    )
    if claimed is not None:
        logger.info("Claimed warm sandbox %s for user %s", claimed.sandbox_id, req.user_id)
        return claimed

    # No warm box available — cold create with the same parameters.
    logger.info("No warm box for template=%s; cold-creating for user %s", template, req.user_id)
    return await create_sandbox(req)


@router.get("", response_model=SandboxListResponse)
async def list_sandboxes(user_id: str | None = None, include_deleted: bool = False):
    """List sandboxes, optionally filtered by user. Soft-deleted rows are
    hidden by default — pass ``include_deleted=true`` for admin/audit views."""
    sandboxes = await sandbox_manager.list_sandboxes(
        user_id=user_id, include_deleted=include_deleted
    )
    return SandboxListResponse(sandboxes=sandboxes, total=len(sandboxes))


@router.get("/{sandbox_id}", response_model=SandboxResponse)
async def get_sandbox(sandbox_id: str):
    """Get sandbox details by ID."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    return sandbox


def _migrating_503(sandbox_id: str) -> HTTPException:
    """Retryable response for a tool call that landed during a migration swap.
    The agent's tool proxy retries onto the new container (same sandbox_id)."""
    return HTTPException(
        status_code=503,
        detail={
            "status": "migrating",
            "sandbox_id": sandbox_id,
            "message": "sandbox is migrating to a new image; retry shortly",
        },
        headers={"Retry-After": "3"},
    )


# Statuses whose container is gone but whose volume survives — a resume brings
# the box back. Tool calls against these used to fall through to a baffling
# 500 ("Could not determine sandbox IP"); the FE needs an actionable signal.
_RESUMABLE_STATUSES = {"expired", "stopped"}
_DEAD_STATUSES = {"failed", "deleted"}


def _require_live(sandbox) -> None:
    """Raise 410 with a resume hint when a TOOL route is called on a sandbox
    whose container is gone. Lifecycle routes (resume/extend/destroy/details)
    must NOT use this — operating on terminal rows is their whole job."""
    status = getattr(sandbox.status, "value", None) or str(sandbox.status)
    if status in _RESUMABLE_STATUSES:
        raise HTTPException(
            status_code=410,
            detail=(
                f"Sandbox {sandbox.sandbox_id} is {status} — its container is gone "
                f"but the user volume is preserved. POST /sandboxes/{sandbox.sandbox_id}/resume "
                "to bring it back, then retry this call."
            ),
        )
    if status in _DEAD_STATUSES:
        raise HTTPException(
            status_code=410,
            detail=f"Sandbox {sandbox.sandbox_id} is {status} and cannot serve tool calls.",
        )


@router.post("/{sandbox_id}/exec", response_model=ExecResponse)
async def exec_command(sandbox_id: str, req: ExecRequest):
    """Execute a command inside a running sandbox."""
    if activity.is_migrating(sandbox_id):
        raise _migrating_503(sandbox_id)
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    _require_live(sandbox)

    try:
        async with activity.track(sandbox_id):
            exit_code, stdout, stderr, cwd = await sandbox_manager.exec_in_sandbox(
                sandbox_id=sandbox_id,
                command=req.command,
                timeout=req.timeout,
                user=req.user,
                cwd=req.cwd,
                env=req.env,
                stdin=req.stdin,
            )
            return ExecResponse(exit_code=exit_code, stdout=stdout, stderr=stderr, cwd=cwd)
    except activity.SandboxMigratingError:
        raise _migrating_503(sandbox_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{sandbox_id}/access", response_model=AccessResponse)
async def request_access(sandbox_id: str):
    """Generate temporary SSH credentials for direct sandbox access.

    Returns a one-time Ed25519 private key and connection details.
    The public key is injected into the running container. The private key
    is never stored — it exists only in this response.
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    try:
        access = await sandbox_manager.generate_user_access(sandbox_id)
        ssh_cmd = (
            f"ssh -i /tmp/{sandbox_id}.pem "
            f"-o StrictHostKeyChecking=no "
            f"-p {access['port']} {access['username']}@{access['host']}"
        )
        return AccessResponse(
            private_key=access["private_key"],
            username=access["username"],
            host=access["host"],
            port=access["port"],
            ssh_command=ssh_cmd,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate access: {e}")


@router.delete("/{sandbox_id}", status_code=204)
async def destroy_sandbox(sandbox_id: str, graceful: bool = True, purge: bool = False):
    """Destroy a sandbox.

    Default: container torn down, row marked stopped — still visible in
    history and resumable until the retention sweep ages it out.
    ``purge=true``: additionally soft-delete the row so it disappears from
    every default list immediately ("delete" in user-facing UIs). The
    per-user volume is preserved either way.
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    success = await sandbox_manager.destroy_sandbox(
        sandbox_id, graceful=graceful, reason="user_requested"
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to destroy sandbox")
    if purge:
        from orchestrator.sandbox_manager import _get_store
        await _get_store().soft_delete(sandbox_id)


@router.post("/{sandbox_id}/reset", response_model=SandboxResponse)
async def reset_sandbox(sandbox_id: str, wipe_volume: bool = False):
    """Destroy + recreate a sandbox with the SAME configuration.

    Used when an image / orchestrator config change means the running
    container is out-of-date and the operator wants the latest version
    without re-typing creation params. Preserves the per-user persistent
    Docker volume (``/home/agent``) by default — pass ``wipe_volume=true``
    to nuke it and start with a fresh home dir.

    Returns the NEW sandbox row (different ``sandbox_id`` because the
    in-memory store generates a new id). Callers must swap their cached
    reference to the returned sandbox.
    """
    old = await sandbox_manager.get_sandbox(sandbox_id)
    if not old:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    # Snapshot creation params before destroy so we don't depend on the
    # store keeping the row alive (some implementations purge on destroy).
    user_id = old.user_id
    tier = getattr(old.tier, "value", old.tier) if old.tier else None
    template = old.template
    template_version = old.template_version
    labels = old.labels
    ttl_seconds = old.ttl_seconds
    config = dict(old.config or {})
    resources = config.get("resources") if isinstance(config.get("resources"), dict) else None

    logger.info(
        "Resetting sandbox %s (user=%s, template=%s, wipe_volume=%s)",
        sandbox_id, user_id, template, wipe_volume,
    )

    # 1. Destroy the existing container (preserves named volume).
    await sandbox_manager.destroy_sandbox(sandbox_id, graceful=True, reason="user_reset")

    # 2. Optional volume wipe — clears the per-user Docker volume so the
    # new sandbox boots with an empty /home/agent.
    if wipe_volume:
        try:
            wiped = await sandbox_manager.delete_user_volume(user_id)
            logger.info("Reset wiped per-user volume for %s: %s", user_id, wiped)
        except Exception as exc:
            logger.warning("Reset volume wipe for %s failed: %s", user_id, exc)

    # 3. Re-create with the same shape.
    try:
        new_sandbox = await sandbox_manager.create_sandbox(
            user_id=user_id,
            config=config,
            template=template,
            template_version=template_version,
            tier=tier,
            resources=resources,
            labels=labels,
            ttl_seconds=ttl_seconds,
        )
    except Exception as exc:
        logger.exception("Reset re-create failed for %s", sandbox_id)
        raise HTTPException(status_code=500, detail=f"Reset re-create failed: {exc}")

    return new_sandbox


@router.post("/{sandbox_id}/resume", response_model=SandboxResponse)
async def resume_sandbox(sandbox_id: str):
    """Bring a stopped / expired sandbox back online.

    This is the other half of the lifecycle the reaper completes. When a
    sandbox expires, the reaper gracefully tears down the container (running
    the final data sync) and keeps the per-user volume. Resume spins a fresh
    container — on the LATEST image — back onto that same volume, so the
    user's data is "put back" exactly where it was. The user's mental model:
    "I go and you put my data back and get it going again."

    Resumable states: ``stopped``, ``expired``, ``failed``. Resume is
    rejected for:
      - a soft-deleted row (409) — that workspace was intentionally
        discarded; create a new sandbox instead.
      - an already-live sandbox (409) — nothing to resume; just use it.

    Like ``/reset``, a NEW ``sandbox_id`` is minted (a fresh row + container).
    The data is identical because the per-user Docker volume is keyed on
    ``user_id``, not ``sandbox_id``. Callers must swap their cached reference
    to the returned sandbox. The old row is left as audit history.
    """
    old = await sandbox_manager.get_sandbox(sandbox_id)
    if not old:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    # Don't resurrect an intentionally-discarded workspace.
    store = sandbox_manager._get_store()
    lifecycle = await store.get_lifecycle(sandbox_id)
    if lifecycle and lifecycle["deleted"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Sandbox {sandbox_id} was deleted — its workspace is gone and "
                "cannot be resumed. Create a new sandbox instead."
            ),
        )

    status = getattr(old.status, "value", old.status)
    _LIVE = {"creating", "starting", "ready", "running", "shutting_down"}
    if status in _LIVE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Sandbox {sandbox_id} is '{status}', not stopped/expired — "
                "it's already active, so there's nothing to resume. Use it directly."
            ),
        )

    # Snapshot the original shape so the resumed sandbox is the same kind of
    # box, just on the latest image. The per-user volume restores the data.
    user_id = old.user_id
    tier = getattr(old.tier, "value", old.tier) if old.tier else None
    template = old.template
    template_version = old.template_version
    labels = old.labels
    ttl_seconds = old.ttl_seconds
    config = dict(old.config or {})
    resources = config.get("resources") if isinstance(config.get("resources"), dict) else None

    logger.info(
        "Resuming sandbox %s (user=%s, template=%s, prior_status=%s)",
        sandbox_id, user_id, template, status,
    )

    try:
        await storage.ensure_user_storage(user_id)
        new_sandbox = await sandbox_manager.create_sandbox(
            user_id=user_id,
            config=config,
            template=template,
            template_version=template_version,
            tier=tier,
            resources=resources,
            labels=labels,
            ttl_seconds=ttl_seconds,
        )
    except Exception as exc:
        logger.exception("Resume re-create failed for %s", sandbox_id)
        raise HTTPException(status_code=500, detail=f"Resume failed: {exc}")

    return new_sandbox


@router.get("/{sandbox_id}/agent-env", tags=["diagnostics"])
async def sandbox_agent_env(sandbox_id: str) -> dict:
    """Return the env vars VISIBLE INSIDE the running sandbox container.

    Renders three views:
      - ``container_config_env``: values baked in at ``docker run`` time
        (from ``Config.Env`` on the container — the orchestrator's
        passthrough output).
      - ``runtime_env``: actual ``env`` output from a fresh shell inside
        the container (catches anything the entrypoint or ttyd injects
        that isn't on Config.Env).
      - ``aidream_proc_env``: env of the running aidream process (PID 1
        inside ``mtx aidream serve``), reflecting what the FastAPI
        process actually sees. Only present if aidream is running.

    Names are returned alphabetically; values are returned verbatim
    because operator-only and isolated to the diagnostics surface.
    Use this to debug 'why doesn't the agent see X?' without guessing.
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    out: dict = {"sandbox_id": sandbox_id}

    try:
        client = sandbox_manager._get_docker_client()
        container = await asyncio.to_thread(client.containers.get, sandbox_id)
        await asyncio.to_thread(container.reload)

        # 1. docker inspect Config.Env — the snapshot at run time
        cfg_env = container.attrs.get("Config", {}).get("Env", []) or []
        out["container_config_env"] = _kv_list_from_env_lines(cfg_env)

        # 2. fresh shell `env` inside container
        try:
            exit_code, output = await asyncio.to_thread(lambda: container.exec_run(["env"], stdout=True, stderr=True))
            text = output.decode(errors="replace") if output else ""
            if exit_code != 0:
                out["runtime_env_error"] = text
            else:
                out["runtime_env"] = _kv_list_from_env_lines(text.splitlines())
        except Exception as exc:
            out["runtime_env_error"] = str(exc)

        # 3. aidream process env via /proc/<pid>/environ — ONLY meaningful when
        #    the aidream server is actually running (the 'aidream' template).
        #    Match 'mtx aidream serve' SPECIFICALLY — NOT a bare 'uvicorn',
        #    because the matrx_agent daemon is itself uvicorn (port 8000). On a
        #    slim box the old 'aidream|uvicorn' pattern matched the daemon, then
        #    failed to read its /proc/<pid>/environ as a non-root exec user
        #    ("cannot open /proc/29/environ: Permission denied"). Reading as
        #    root (the orchestrator owns the container; this is an operator-only
        #    diagnostics surface) makes any pid's environ readable, and the
        #    narrower match means slim boxes get a clean "not running" note
        #    instead of a scary permission error.
        try:
            pid_code, pid_out = await asyncio.to_thread(lambda: container.exec_run(
                ["sh", "-lc", "pgrep -f 'aidream serve' | head -1"],
                stdout=True, stderr=True, user="root",
            ))
            pid_text = (pid_out.decode(errors="replace") if pid_out else "").strip()
            if pid_code == 0 and pid_text.isdigit():
                env_code, env_out = await asyncio.to_thread(lambda: container.exec_run(
                    ["sh", "-lc", f"tr '\\0' '\\n' < /proc/{pid_text}/environ"],
                    stdout=True, stderr=True, user="root",
                ))
                if env_code == 0:
                    out["aidream_pid"] = int(pid_text)
                    out["aidream_proc_env"] = _kv_list_from_env_lines(
                        env_out.decode(errors="replace").splitlines()
                    )
                else:
                    out["aidream_proc_env_error"] = (
                        env_out.decode(errors="replace") if env_out else ""
                    )
            else:
                # Not an error — slim/bare templates simply don't run aidream.
                out["aidream_proc_env_note"] = (
                    "aidream server not running on this template "
                    f"(template={sandbox.template or 'bare'}); this view only "
                    "applies to the 'aidream' template. The agent's env is in "
                    "container_config_env + runtime_env above."
                )
        except Exception as exc:
            out["aidream_proc_env_error"] = str(exc)

    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot inspect container: {exc}")

    return out


@router.post("/{sandbox_id}/migrate")
async def migrate_sandbox_route(sandbox_id: str, target_image: str | None = None):
    """Zero-drift migration: swap this box onto the current image for its
    template, keeping the SAME sandbox_id and per-user volume (data intact).

    Unlike /resume and /reset, the sandbox_id does NOT change — the agent's
    existing binding (``/sandboxes/<id>``) stays valid across the swap, which is
    what lets a chat suspend, migrate, and resume in place. The CALLER must
    quiesce the chat first (aidream parks the turn); this primitive verifies
    readiness + version before cutover and rolls back to the old box on failure.
    Master-key only (global APIKeyMiddleware)."""
    from orchestrator.migrate import migrate_sandbox

    store = sandbox_manager._get_store()
    result = await migrate_sandbox(sandbox_id, store=store, target_image=target_image)
    if result["status"] in ("migrated", "already_current"):
        return result
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    # failed — the OLD box is still running; surface loudly (502, not 500, so the
    # caller knows the box is intact and it can keep using the old version).
    raise HTTPException(status_code=502, detail=result)


@router.post("/{sandbox_id}/heartbeat", response_model=HeartbeatResponse)
async def sandbox_heartbeat(sandbox_id: str):
    """Record a heartbeat from a sandbox."""
    ack = await sandbox_manager.heartbeat(sandbox_id)
    if not ack:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    return HeartbeatResponse(acknowledged=True, sandbox_id=sandbox_id)


@router.post("/{sandbox_id}/complete", response_model=CompletionResponse)
async def sandbox_complete(sandbox_id: str, req: CompletionRequest | None = None):
    """Agent signals that its task is complete. Triggers graceful shutdown."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    logger.info("Sandbox %s signaled completion", sandbox_id)
    await sandbox_manager.destroy_sandbox(sandbox_id, graceful=True, reason="graceful_shutdown")
    return CompletionResponse(status="shutting_down", sandbox_id=sandbox_id)


@router.post("/{sandbox_id}/error", response_model=ErrorResponse)
async def sandbox_error(sandbox_id: str, req: ErrorReport):
    """Agent signals an error. Logs the error and triggers graceful shutdown."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    logger.error(
        "Sandbox %s (user=%s) reported error: %s",
        sandbox_id, sandbox.user_id, req.error,
    )

    await sandbox_manager.destroy_sandbox(sandbox_id, graceful=True, reason="error")
    return ErrorResponse(status="shutting_down", sandbox_id=sandbox_id, error_received=True)


@router.post("/{sandbox_id}/extend", response_model=ExtendResponse)
async def extend_sandbox(
    sandbox_id: str,
    req: ExtendRequest | None = None,
    ttl_seconds: int | None = None,
):
    """Extend the TTL of a sandbox.

    Accepts either a JSON body (``{"ttl_seconds": 3600}``) or a query param
    (``?ttl_seconds=3600``) for backward compatibility. Persists the new
    ``expires_at`` so the orchestrator's expiry sweep won't shut the sandbox
    down prematurely.
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    seconds = (req.ttl_seconds if req else None) or ttl_seconds or 3600
    if seconds < 60 or seconds > 86400:
        raise HTTPException(status_code=400, detail="ttl_seconds must be between 60 and 86400")

    store = sandbox_manager._get_store()
    new_expires_at = await store.extend_ttl(sandbox_id, seconds)
    if not new_expires_at:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    logger.info("Extended sandbox %s by %ds (new expires_at=%s)", sandbox_id, seconds, new_expires_at)
    return ExtendResponse(
        sandbox_id=sandbox_id,
        ttl_seconds=seconds,
        expires_at=new_expires_at,
        new_expires_at=new_expires_at,
    )


def _authenticate_websocket(websocket: WebSocket, sandbox_id: str, required_scope: str) -> bool:
    """Auth gate for the WebSocket tool routes (/pty, /fs/watch).

    The HTTP APIKeyMiddleware is a BaseHTTPMiddleware and never sees WebSocket
    connections, so these routes have to authenticate themselves or they are an
    open, unauthenticated terminal/file-watch into any sandbox by id. Accepts:

      * master ``X-API-Key`` (header) or ``?api_key=`` query param, OR
      * a sandbox-scoped token bound to ``sandbox_id`` carrying ``required_scope``
        — supplied via ``X-Sandbox-Access-Token`` / ``Authorization: Bearer``
        header, or (since browsers can't set WS headers) a ``?token=`` /
        ``?access_token=`` query param.

    When no master key is configured the orchestrator is in local-dev unauth
    mode (mirrors the HTTP middleware), so connections are allowed.
    """
    if not settings.api_key:
        return True  # local dev: middleware allows all HTTP too

    qp = websocket.query_params

    # Master key (header or query param)
    master = websocket.headers.get(settings.api_key_header) or qp.get("api_key")
    if master and hmac.compare_digest(master, settings.api_key):
        return True

    # Sandbox-scoped token (header or query param)
    token = (
        websocket.headers.get("x-sandbox-access-token")
        or websocket.headers.get("X-Sandbox-Access-Token")
        or qp.get("token")
        or qp.get("access_token")
    )
    if not token:
        auth_header = websocket.headers.get("authorization") or websocket.headers.get("Authorization") or ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
    if not token or not settings.access_token_secret:
        return False
    try:
        payload = sandbox_token.verify_token(
            token=token,
            secret=settings.access_token_secret,
            expected_sandbox_id=sandbox_id,
            required_scope=required_scope,
        )
    except sandbox_token.TokenError:
        return False
    # Spend a single-use token now that it has authorized this connection. This
    # is the WebSocket-upgrade consumption point the token contract describes;
    # without it, single_use=True was decorative (the token stayed replayable).
    if payload.get("single_use"):
        jti = payload.get("jti")
        if jti:
            sandbox_token.consume_jti(jti)
    return True


@router.websocket("/{sandbox_id}/fs/watch")
async def proxy_fs_watch(sandbox_id: str, websocket: WebSocket):
    """Proxy WebSocket for file watching to the internal sandbox daemon."""
    if not _authenticate_websocket(websocket, sandbox_id, required_scope="fs.watch"):
        await websocket.close(code=1008, reason="Unauthorized: master key or fs.watch-scoped token required")
        return
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        await websocket.close(code=1008, reason=f"Sandbox {sandbox_id} not found")
        return
    _status = getattr(sandbox.status, "value", None) or str(sandbox.status)
    if _status in _RESUMABLE_STATUSES | _DEAD_STATUSES:
        # Actionable close instead of the generic 1011 "Could not determine
        # sandbox IP" the dead container would otherwise produce downstream.
        await websocket.close(code=1008, reason=f"Sandbox {sandbox_id} is {_status} — resume it and reconnect")
        return

    container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        await websocket.close(code=1011, reason="Could not determine sandbox IP")
        return

    import websockets
    from websockets.exceptions import ConnectionClosed
    import asyncio
    
    await websocket.accept()
    # An attached PTY/watch session marks the box BUSY for the rolling
    # auto-migrator — never swap a container out from under an open terminal
    # or editor. Balanced by session_closed in this handler's finally.
    activity.session_opened(sandbox_id)

    params = str(websocket.query_params)
    agent_tok = sandbox_manager.agent_token_for(sandbox_id)
    if agent_tok:
        params = (params + "&" if params else "") + f"agent_token={agent_tok}"
    ws_url = f"ws://{container_ip}:8000/fs/watch"
    if params:
        ws_url += f"?{params}"
        
    try:
        async with websockets.connect(ws_url) as client_ws:
            async def forward_to_client():
                try:
                    while True:
                        msg = await client_ws.recv()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except ConnectionClosed:
                    pass
                except Exception:
                    pass

            async def forward_to_sandbox():
                try:
                    while True:
                        msg = await websocket.receive()
                        if "text" in msg:
                            await client_ws.send(msg["text"])
                        elif "bytes" in msg:
                            await client_ws.send(msg["bytes"])
                except Exception:
                    pass

            task1 = asyncio.create_task(forward_to_client())
            task2 = asyncio.create_task(forward_to_sandbox())

            done, pending = await asyncio.wait(
                [task1, task2],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}")
    finally:
        activity.session_closed(sandbox_id)
        try:
            await websocket.close()
        except:
            pass


@router.api_route("/{sandbox_id}/fs/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_fs(sandbox_id: str, path: str, request: Request):
    """Proxy file system requests to the internal sandbox daemon."""
    if activity.is_migrating(sandbox_id):
        raise _migrating_503(sandbox_id)
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    _require_live(sandbox)

    try:
        async with activity.track(sandbox_id):
            container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
            if not container_ip:
                raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

            # Forward the request
            url = f"http://{container_ip}:8000/fs/{path}"

            # We use httpx.AsyncClient to forward the request
            async with httpx.AsyncClient() as client:
                # Read the body if it exists
                body = await request.body()

                # Forward the query parameters
                params = request.query_params

                try:
                    resp = await client.request(
                        method=request.method,
                        url=url,
                        params=params,
                        content=body,
                        headers=_with_agent({k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}, sandbox_id),
                        timeout=60.0
                    )
                except httpx.RequestError as exc:
                    raise HTTPException(status_code=502, detail=f"Error proxying request: {exc}")

                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers=dict(resp.headers)
                )
    except activity.SandboxMigratingError:
        raise _migrating_503(sandbox_id)


@router.api_route("/{sandbox_id}/exec/stream", methods=["POST"])
async def proxy_exec_stream(sandbox_id: str, request: Request):
    """Proxy streaming exec requests to the internal sandbox daemon."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    _require_live(sandbox)
    if activity.is_migrating(sandbox_id):
        raise _migrating_503(sandbox_id)

    container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

    url = f"http://{container_ip}:8000/exec/stream"

    from fastapi.responses import StreamingResponse

    async def stream_generator():
        # Count the whole stream as in-flight so a migration drains/defers
        # rather than cutting over mid-stream.
        try:
            async with activity.track(sandbox_id):
                async with httpx.AsyncClient() as client:
                    body = await request.body()
                    async with client.stream(
                        method=request.method,
                        url=url,
                        content=body,
                        headers=_with_agent({k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}, sandbox_id),
                        timeout=None
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
        except activity.SandboxMigratingError:
            return  # migration began in the race window; client retries the call

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


@router.api_route("/{sandbox_id}/git/{path:path}", methods=["GET", "POST"])
async def proxy_git(sandbox_id: str, path: str, request: Request):
    """Proxy git requests to the internal sandbox daemon."""
    if activity.is_migrating(sandbox_id):
        raise _migrating_503(sandbox_id)
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    _require_live(sandbox)

    try:
        async with activity.track(sandbox_id):
            container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
            if not container_ip:
                raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

            url = f"http://{container_ip}:8000/git/{path}"

            async with httpx.AsyncClient() as client:
                body = await request.body()
                params = request.query_params

                try:
                    resp = await client.request(
                        method=request.method,
                        url=url,
                        params=params,
                        content=body,
                        headers=_with_agent({k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}, sandbox_id),
                        timeout=120.0 # Git clones can take a while
                    )
                except httpx.RequestError as exc:
                    raise HTTPException(status_code=502, detail=f"Error proxying request: {exc}")

                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers=dict(resp.headers)
                )
    except activity.SandboxMigratingError:
        raise _migrating_503(sandbox_id)


@router.api_route("/{sandbox_id}/credentials", methods=["POST"])
@router.api_route("/{sandbox_id}/credentials/revoke", methods=["POST"])
async def proxy_credentials(sandbox_id: str, request: Request):
    """Proxy credentials requests to the internal sandbox daemon."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    _require_live(sandbox)
        
    container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

    # The path will be either /credentials or /credentials/revoke.
    # Split on the full "/{sandbox_id}/" segment (not the bare id, which could
    # appear elsewhere in the URL) so reconstruction is unambiguous.
    _, _, suffix = request.url.path.partition(f"/{sandbox_id}/")
    url = f"http://{container_ip}:8000/{suffix}"
    
    async with httpx.AsyncClient() as client:
        body = await request.body()
        params = request.query_params
        
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                params=params,
                content=body,
                headers=_with_agent({k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}, sandbox_id),
                timeout=60.0
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Error proxying request: {exc}")

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers)
        )


@router.websocket("/{sandbox_id}/pty")
async def proxy_pty(sandbox_id: str, websocket: WebSocket):
    """Proxy PTY WebSocket to the internal sandbox daemon."""
    if not _authenticate_websocket(websocket, sandbox_id, required_scope="pty"):
        await websocket.close(code=1008, reason="Unauthorized: master key or pty-scoped token required")
        return
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        await websocket.close(code=1008, reason=f"Sandbox {sandbox_id} not found")
        return
    _status = getattr(sandbox.status, "value", None) or str(sandbox.status)
    if _status in _RESUMABLE_STATUSES | _DEAD_STATUSES:
        # Actionable close instead of the generic 1011 "Could not determine
        # sandbox IP" the dead container would otherwise produce downstream.
        await websocket.close(code=1008, reason=f"Sandbox {sandbox_id} is {_status} — resume it and reconnect")
        return

    container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        await websocket.close(code=1011, reason="Could not determine sandbox IP")
        return

    # Forward the WebSocket connection using websockets library
    import websockets
    from websockets.exceptions import ConnectionClosed
    import asyncio
    
    await websocket.accept()
    # An attached PTY/watch session marks the box BUSY for the rolling
    # auto-migrator — never swap a container out from under an open terminal
    # or editor. Balanced by session_closed in this handler's finally.
    activity.session_opened(sandbox_id)

    params = str(websocket.query_params)
    agent_tok = sandbox_manager.agent_token_for(sandbox_id)
    if agent_tok:
        params = (params + "&" if params else "") + f"agent_token={agent_tok}"
    ws_url = f"ws://{container_ip}:8000/pty"
    if params:
        ws_url += f"?{params}"
        
    try:
        async with websockets.connect(ws_url) as client_ws:
            async def forward_to_client():
                try:
                    while True:
                        msg = await client_ws.recv()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except ConnectionClosed:
                    pass
                except Exception:
                    pass

            async def forward_to_sandbox():
                try:
                    while True:
                        msg = await websocket.receive()
                        if "text" in msg:
                            await client_ws.send(msg["text"])
                        elif "bytes" in msg:
                            await client_ws.send(msg["bytes"])
                except Exception:
                    pass

            task1 = asyncio.create_task(forward_to_client())
            task2 = asyncio.create_task(forward_to_sandbox())

            done, pending = await asyncio.wait(
                [task1, task2],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}")
    finally:
        activity.session_closed(sandbox_id)
        try:
            await websocket.close()
        except:
            pass

@router.api_route("/{sandbox_id}/search/{path:path}", methods=["GET", "POST"])
async def proxy_search(sandbox_id: str, path: str, request: Request):
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    _require_live(sandbox)
        
    container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

    url = f"http://{container_ip}:8000/search/{path}"
    
    async with httpx.AsyncClient() as client:
        body = await request.body()
        params = request.query_params
        
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                params=params,
                content=body,
                headers=_with_agent({k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}, sandbox_id),
                timeout=60.0
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Error proxying request: {exc}")

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers)
        )

@router.api_route("/{sandbox_id}/processes", methods=["GET"])
@router.api_route("/{sandbox_id}/processes/{pid:int}/signal", methods=["POST"])
async def proxy_processes(sandbox_id: str, request: Request, pid: int = None):
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    _require_live(sandbox)
        
    container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

    # Reconstruct the path after the "/{sandbox_id}/" segment unambiguously.
    _, _, suffix = request.url.path.partition(f"/{sandbox_id}/")
    url = f"http://{container_ip}:8000/{suffix}"
    
    async with httpx.AsyncClient() as client:
        body = await request.body()
        params = request.query_params
        
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                params=params,
                content=body,
                headers=_with_agent({k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}, sandbox_id),
                timeout=10.0
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Error proxying request: {exc}")

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers)
        )

@router.api_route("/{sandbox_id}/ports", methods=["GET"])
async def proxy_ports(sandbox_id: str, request: Request):
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    _require_live(sandbox)
        
    container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

    url = f"http://{container_ip}:8000/ports"

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=_with_agent({k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}, sandbox_id),
                timeout=10.0
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Error proxying request: {exc}")

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers)
        )


# ─── Browser-direct access tokens + reverse proxy ────────────────────────────
# §2 + §7 of features/code/SANDBOX_DIRECT_ENDPOINTS.md (matrx-frontend).
# Lets the browser hit the orchestrator directly for SSE / WebSocket / large
# transfers / AI passthrough — bypassing Vercel's 300s function cap and WS
# limitations. Auth is short-lived HMAC bearer tokens minted by Next.js
# (admin-key-authed); the proxy validates each token against the sandbox id
# in the path, scope, and expiry before forwarding to the in-container
# matrx_agent daemon.
#
# Two routes:
#   POST /sandboxes/{sandbox_id}/access-tokens  — admin-key-authed, mints token
#   ANY  /sandboxes/{sandbox_id}/proxy/{path:path} — bearer- or admin-authed reverse proxy

# Headers we proxy-side own (proxy auth, hop-by-hop) and must NEVER forward
# to the in-container daemon. Notably ``authorization`` is INTENTIONALLY
# NOT in this set — it carries the upstream identity (the user's Supabase
# JWT, an aidream API token, etc.) and the daemon needs it to identify the
# request. Stripping it was the cause of the "Conversation not found" 404
# we saw when the FE switched to the proxy_url path.
_HOP_BY_HOP_FORWARD = {
    "host", "content-length", "connection", "transfer-encoding", "upgrade",
    "x-api-key",                   # master proxy auth — proxy-only
    "x-sandbox-access-token",      # scoped proxy auth — proxy-only
}

# Headers we strip from the upstream response on the way back. Standard
# hop-by-hop set; ``connection`` + ``transfer-encoding`` would make
# starlette unhappy if forwarded verbatim.
_HOP_BY_HOP_BACK = {"transfer-encoding", "content-encoding", "content-length", "connection"}


def _bearer_from_headers(headers) -> str | None:
    raw = headers.get("authorization") or headers.get("Authorization")
    if not raw:
        return None
    if not raw.lower().startswith("bearer "):
        return None
    return raw.split(" ", 1)[1].strip()


def _authenticate_proxy_request(
    request: Request,
    sandbox_id: str,
    *,
    required_scope: str = "ai",
) -> tuple[str, bool]:
    """Authenticate a /proxy/{path:path} call. Returns ``(kind, strip_authorization)``.

    Auth slots, checked in order:

    1. ``X-API-Key`` — master admin key (Next.js, ops). Stripped on forward.
       ``Authorization`` is left untouched.
    2. ``X-Sandbox-Access-Token`` — sandbox-scoped HMAC token issued by
       ``POST /access-tokens``. Recommended for browsers because it doesn't
       collide with the upstream identity carried in ``Authorization``.
       Stripped on forward; ``Authorization`` left untouched.
    3. ``Authorization: Bearer <token>`` where the token validates as our
       HMAC. Legacy compat for callers that already set Authorization to
       our token. Consumed by us → stripped on forward.
    4. ``Authorization: Bearer <anything else>`` — treated as upstream
       identity (Supabase JWT, aidream API token, etc.). The daemon is the
       identity boundary; we forward unchanged. The orchestrator does NOT
       try to validate JWTs here — that responsibility belongs to the
       upstream service, which already does it correctly.

    Returns ``(kind, strip_authorization)`` where ``strip_authorization``
    tells the proxy whether to drop the inbound ``Authorization`` from the
    forwarded headers. False for cases (1), (2), (4); True only for (3).

    A request with NO recognised auth (no master, no scoped, no bearer)
    is rejected with 401 to keep the proxy from being an open relay.
    """
    # 1. Master key
    if settings.api_key:
        master_candidate = request.headers.get(settings.api_key_header)
        if master_candidate and hmac.compare_digest(master_candidate, settings.api_key):
            return "master", False

    # 2. Sandbox-scoped token via dedicated header (preferred for browsers)
    scoped_header_token = request.headers.get("x-sandbox-access-token") or request.headers.get(
        "X-Sandbox-Access-Token"
    )
    if scoped_header_token:
        if not settings.access_token_secret:
            raise HTTPException(
                status_code=503,
                detail="access tokens not configured (set MATRX_ACCESS_TOKEN_SECRET)",
            )
        try:
            sandbox_token.verify_token(
                token=scoped_header_token,
                secret=settings.access_token_secret,
                expected_sandbox_id=sandbox_id,
                required_scope=required_scope,
            )
        except sandbox_token.TokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return "scoped-header", False

    # 3+4. Authorization Bearer — could be our HMAC (legacy) or a foreign
    # token destined for the upstream daemon (Supabase JWT, etc.).
    auth_bearer = _bearer_from_headers(request.headers)
    if auth_bearer:
        # Try to verify it as our HMAC token. Success → we own it; strip.
        if settings.access_token_secret:
            try:
                sandbox_token.verify_token(
                    token=auth_bearer,
                    secret=settings.access_token_secret,
                    expected_sandbox_id=sandbox_id,
                    required_scope=required_scope,
                )
                return "scoped-bearer", True
            except sandbox_token.TokenError:
                # Not our token — treat as opaque upstream identity. The
                # daemon validates it; the orchestrator just forwards.
                pass
        return "passthrough-bearer", False

    raise HTTPException(
        status_code=401,
        detail=(
            "no auth provided — supply one of: "
            "X-API-Key (master); "
            "X-Sandbox-Access-Token: <token from POST /access-tokens> (scoped); "
            "Authorization: Bearer <jwt-or-upstream-token> (forwarded to the in-container daemon)"
        ),
    )


@router.post("/{sandbox_id}/access-tokens", response_model=AccessTokenResponse)
async def issue_access_token(sandbox_id: str, body: AccessTokenRequest) -> AccessTokenResponse:
    """Mint a short-lived HMAC bearer token bound to ``sandbox_id``.

    Admin-authed (via the global APIKeyMiddleware — Next.js calls this after
    verifying the user owns the sandbox). The token is presented as
    ``Authorization: Bearer <token>`` on browser-direct calls to
    ``/sandboxes/{sandbox_id}/proxy/*``.

    Returns 503 when the orchestrator hasn't been configured with an HMAC
    secret (set ``MATRX_ACCESS_TOKEN_SECRET``).
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    if not settings.access_token_secret:
        raise HTTPException(
            status_code=503,
            detail="access tokens not configured (set MATRX_ACCESS_TOKEN_SECRET on the orchestrator)",
        )

    ttl = body.ttl_seconds or sandbox_token.DEFAULT_TTL_SECONDS

    try:
        token, payload = sandbox_token.issue_token(
            secret=settings.access_token_secret,
            sandbox_id=sandbox_id,
            scopes=body.scopes,
            tier=settings.host_tier or (sandbox.tier.value if sandbox.tier else "ec2"),
            ttl_seconds=ttl,
            actor=body.actor,
            single_use=body.single_use,
        )
    except sandbox_token.TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    direct_url = (settings.public_url or "").rstrip("/")
    if not direct_url:
        raise HTTPException(
            status_code=503,
            detail="MATRX_PUBLIC_URL must be set for the orchestrator to advertise its direct URL",
        )
    ws_base = direct_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)

    return AccessTokenResponse(
        token=token,
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        direct_url=direct_url,
        ws_base=ws_base,
        tier=payload["tier"],
        sandbox_id=sandbox_id,
    )


@router.post("/{sandbox_id}/agent-binding")
async def agent_binding(sandbox_id: str, body: AgentBindingRequest | None = None) -> dict:
    """Return the exact ``active_sandbox`` binding the aidream agent expects.

    This is the turnkey handoff primitive: one call mints a scoped token AND
    assembles the binding, so the frontend drops the returned object straight
    into a chat request's ``sandbox`` field and the agent's filesystem/shell/
    git tools then execute INSIDE this box. No client-side URL assembly, no
    guessing the scope set.

    The returned shape matches matrx-ai's ``_sandbox_proxy.SandboxBinding``:
        { sandbox_id, base_url, access_token, root_path, expires_at }

    Admin-authed via the global middleware (Next.js calls this after verifying
    the user owns the sandbox).
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    if not settings.access_token_secret:
        raise HTTPException(status_code=503, detail="access tokens not configured (set MATRX_ACCESS_TOKEN_SECRET)")
    # Prefer the internal/in-VPC address so the co-located AI Dream's tool
    # calls stay on the private LAN (same-AZ = free + sub-ms). On EC2 this is
    # auto-detected from instance metadata — no operator config needed. Falls
    # back to MATRX_PUBLIC_URL off-EC2 / when unresolved.
    base = sandbox_manager.resolve_internal_base().rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="Could not resolve a base URL (set MATRX_INTERNAL_URL or MATRX_PUBLIC_URL)")

    ttl = (body.ttl_seconds if body else None) or settings.max_session_duration_seconds
    # The agent's hands need the full tool surface. The middleware now enforces a
    # per-subpath scope (see _required_scope_for), so this default set must cover
    # every structured tool route; it also satisfies the /proxy/* "ai" scope.
    scopes = (body.scopes if body and body.scopes else None) or [
        "ai", "exec.run", "exec.stream", "fs.read", "fs.write", "fs.watch",
        "git", "ports.read", "processes.read", "pty",
    ]
    try:
        token, payload = sandbox_token.issue_token(
            secret=settings.access_token_secret,
            sandbox_id=sandbox_id,
            scopes=scopes,
            tier=settings.host_tier or (sandbox.tier.value if sandbox.tier else "ec2"),
            ttl_seconds=ttl,
            # Server-to-server binding (co-located AI Dream): allow a full
            # session, not the 15-min browser ceiling, so a long agent run
            # isn't silently cut off mid-turn.
            max_ttl_seconds=settings.max_session_duration_seconds,
        )
    except sandbox_token.TokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "sandbox_id": sandbox_id,
        "base_url": f"{base}/sandboxes/{sandbox_id}",
        "access_token": token,
        "root_path": sandbox.hot_path or "/home/agent",
        "expires_at": datetime.fromtimestamp(payload["exp"], tz=timezone.utc).isoformat(),
    }


@router.api_route(
    "/{sandbox_id}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_to_container(sandbox_id: str, path: str, request: Request):
    """Reverse-proxy arbitrary HTTP requests into the in-container daemon.

    Browser → ``{public_url}/sandboxes/{sandbox_id}/proxy/<path>`` →
    orchestrator validates token → forwards to ``http://<container_ip>:8000/<path>``
    1:1 (method, headers minus hop-by-hop, body, response shape, streaming).

    Used by the React `code` workspace's per-conversation
    ``serverOverrideUrl`` to route AI-passthrough calls (``/ai/agents/.../execute``,
    NDJSON streams, etc.) without traversing Next.js. Auth: Bearer token from
    ``POST /access-tokens`` OR ``X-API-Key`` master.
    """
    # OPTIONS preflight handled by the global CORSMiddleware before reaching
    # us; the route still has to declare the method so FastAPI's router
    # doesn't 405. We do a fast-pass here in case CORSMiddleware was
    # misconfigured — return 204 with no upstream call.
    if request.method == "OPTIONS":
        return Response(status_code=204)

    _kind, strip_authorization = _authenticate_proxy_request(
        request, sandbox_id, required_scope="ai"
    )

    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    _require_live(sandbox)

    container_ip = await sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=502, detail="Sandbox container has no reachable IP")

    # Path-based port routing inside the container.
    #
    # Two services run side-by-side in the aidream-image variant:
    #   port 8000 — matrx_agent: low-level sandbox surface
    #               /fs/*, /git/*, /exec/stream, /processes, /ports,
    #               /credentials, /internal/*, etc.
    #   port 8001 — aidream FastAPI (when the image is :aidream and aidream
    #               serve has auto-started): the full /ai/* + /api/* surface
    #               that the FE's chat / agent runs hit.
    #
    # The bare matrx-sandbox:core image has nothing on 8001; calls there
    # 502 with "container has no reachable IP" — which is correct, since
    # AI passthrough only makes sense on the aidream variant.
    upstream_port = 8001 if path.startswith(("ai/", "api/")) else 8000
    target_url = f"http://{container_ip}:{upstream_port}/{path}"
    # Strip Authorization only when the orchestrator itself consumed it
    # (kind="scoped-bearer"). For master / scoped-header / passthrough-bearer
    # we forward Authorization unchanged so the upstream daemon can use it.
    forward_drop = _HOP_BY_HOP_FORWARD | ({"authorization"} if strip_authorization else set())
    forward_headers = _with_agent(
        {k: v for k, v in request.headers.items() if k.lower() not in forward_drop},
        sandbox_id,
    )
    body = await request.body()

    # Use a long-lived client kept open across the streaming response so
    # the upstream connection isn't closed mid-iteration. The body iterator
    # is responsible for closing both the response and the client.
    client = httpx.AsyncClient(timeout=None)
    try:
        upstream_request = client.build_request(
            method=request.method,
            url=target_url,
            content=body,
            headers=forward_headers,
            params=dict(request.query_params),
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    async def body_iterator():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP_BACK
    }
    response_headers["X-Sandbox-Id"] = sandbox_id
    if sandbox.tier:
        # sandbox.tier is a SandboxTier enum coming straight from create_sandbox,
        # but the in-memory store re-serializes it to a plain string on round-trip.
        # Handle both shapes.
        response_headers["X-Tier"] = getattr(sandbox.tier, "value", sandbox.tier)

    return StreamingResponse(
        body_iterator(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )



# ─── Per-sandbox diagnostics + log streaming ────────────────────────────────
# These endpoints exist so the FE can show "is this sandbox actually ready
# for AI passthrough" and surface live logs from inside, instead of the
# black-box situation we hit during sandbox-mode bring-up. Both use the
# orchestrator's master X-API-Key (called from Next.js, never exposed to
# the browser).

import asyncio  # noqa: E402  (late import — only needed for log streaming)


def _kv_list_from_env_lines(lines: list[str]) -> list[dict[str, str]]:
    """Turn a list of ``KEY=value`` strings into ``[{key, value}]`` records,
    sorted by key. Skips lines without ``=``. Used by the agent-env endpoint
    so the FE renders a stable, alphabetised view across all three sources.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in lines:
        if not raw or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "value": value})
    out.sort(key=lambda r: r["key"])
    return out


async def _check_url(url: str, timeout: float = 2.0) -> dict[str, object]:
    """Probe an HTTP endpoint, return {ok, status, error?, latency_ms}."""
    import time
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return {
                "ok": 200 <= resp.status_code < 500,  # 401/403 still mean "service alive"
                "status": resp.status_code,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "body_preview": resp.text[:300] if resp.headers.get("content-type", "").startswith(("text/", "application/json")) else None,
            }
    except httpx.TimeoutException:
        return {"ok": False, "error": "timeout", "latency_ms": int((time.monotonic() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "latency_ms": int((time.monotonic() - started) * 1000)}


@router.get("/{sandbox_id}/diagnostics", tags=["diagnostics"])
async def sandbox_diagnostics(sandbox_id: str) -> dict:
    """End-to-end readiness check for a sandbox. Returns a single JSON
    structure the FE can render verbatim — every layer is reported even on
    failure so the operator sees exactly which piece is broken.

    Sections:

      - sandbox: orchestrator's view (status, container_id, tier, template,
        proxy_url, last_heartbeat, expires_at, persistence_volume)
      - container: docker inspect summary (running/exited, started_at,
        health, network IP, env-var sample)
      - matrx_agent (port 8000): probe /health → http_status + latency
      - aidream (port 8001, only when template=aidream): probe /api/health
        AND /api/health/ready → http_status + latency + body preview
      - passthrough_env: which env vars the orchestrator forwarded into
        this container vs which it tried to and couldn't (so the operator
        knows whether eg SUPABASE_MATRIX_JWT_SECRET arrived)
      - overall: True only if every required layer is ok
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    template = sandbox.template or ""
    is_aidream = (template == "aidream")

    # 1. Container inspect
    container_info: dict[str, object] = {"present": False}
    try:
        client = sandbox_manager._get_docker_client()
        container = await asyncio.to_thread(client.containers.get, sandbox_id)
        await asyncio.to_thread(container.reload)
        attrs = container.attrs
        state = attrs.get("State", {})
        net = attrs.get("NetworkSettings", {}).get("Networks", {})
        net_first = next(iter(net.values()), {}) if net else {}
        # Sample of which passthrough vars made it (names only, never values)
        env_list = attrs.get("Config", {}).get("Env", []) or []
        env_keys_in_container = sorted({e.split("=", 1)[0] for e in env_list if "=" in e})
        passthrough_keys = sandbox_manager._resolve_passthrough_keys()
        env_passthrough_landed = sorted(set(env_keys_in_container) & set(passthrough_keys))
        env_passthrough_missing = sorted(set(passthrough_keys) - set(env_keys_in_container))
        container_info = {
            "present": True,
            "running": state.get("Running", False),
            "status": state.get("Status"),
            "health": (state.get("Health") or {}).get("Status"),
            "started_at": state.get("StartedAt"),
            "exit_code": state.get("ExitCode"),
            "container_ip": net_first.get("IPAddress"),
            "image": attrs.get("Config", {}).get("Image"),
            "passthrough_landed": env_passthrough_landed,
            "passthrough_missing_count": len(env_passthrough_missing),
            "passthrough_missing_sample": env_passthrough_missing[:10],
        }
    except Exception as exc:
        container_info["error"] = str(exc)

    container_ip = container_info.get("container_ip")
    matrx_agent: dict[str, object] = {"checked": False, "reason": "no container ip"}
    aidream_health: dict[str, object] = {"checked": False, "reason": "not aidream template"}
    aidream_ready: dict[str, object] = {"checked": False, "reason": "not aidream template"}

    if container_ip:
        # 2. matrx_agent (always present — fs/git/exec daemon)
        matrx_agent = await _check_url(f"http://{container_ip}:8000/health")
        matrx_agent["checked"] = True

        # 3. aidream (only on the aidream variant) — both /api/health and /api/health/ready
        if is_aidream:
            aidream_health = await _check_url(f"http://{container_ip}:8001/api/health")
            aidream_health["checked"] = True
            aidream_ready = await _check_url(f"http://{container_ip}:8001/api/health/ready")
            aidream_ready["checked"] = True

    # 4. Overall
    overall_ok = bool(
        container_info.get("running")
        and matrx_agent.get("ok")
        and (not is_aidream or (aidream_health.get("ok") and aidream_ready.get("ok")))
    )

    # Secrets-vault injection diagnostic — stamped by create_sandbox at
    # boot time onto sandbox.config.secrets_injection. Forwarded verbatim
    # so the UI shows exactly what happened (attempted? skipped reason?
    # fetched count? error?) — without needing orchestrator log access.
    secrets_injection: dict[str, object] = {}
    try:
        cfg = sandbox.config if isinstance(sandbox.config, dict) else {}
        si = cfg.get("secrets_injection") if isinstance(cfg, dict) else None
        if isinstance(si, dict):
            secrets_injection = dict(si)  # safe copy
            # We don't have the fetched key list (only the count), so the
            # best we can do here is surface the count + the "see container
            # env" pointer. The detail page can cross-reference.
    except Exception as exc:
        secrets_injection = {"error": f"couldn't read sandbox.config: {exc}"}

    return {
        "sandbox_id": sandbox_id,
        "overall_ok": overall_ok,
        "sandbox": {
            "status": getattr(sandbox.status, "value", sandbox.status),
            "tier": getattr(sandbox.tier, "value", sandbox.tier),
            "template": sandbox.template,
            "template_version": sandbox.template_version,
            "user_id": sandbox.user_id,
            "container_id": sandbox.container_id,
            "proxy_url": sandbox.proxy_url,
            "ssh_port": sandbox.ssh_port,
            "expires_at": sandbox.expires_at.isoformat() if sandbox.expires_at else None,
            "last_heartbeat_at": getattr(sandbox, "last_heartbeat_at", None),
            "persistence_volume": sandbox.persistence_volume,
            "hot_path": sandbox.hot_path,
            "cold_path": sandbox.cold_path,
        },
        "container": container_info,
        "checks": {
            "matrx_agent_8000": matrx_agent,
            "aidream_health_8001": aidream_health,
            "aidream_ready_8001": aidream_ready,
        },
        "secrets_injection": secrets_injection,
    }


@router.get("/{sandbox_id}/logs", tags=["diagnostics"])
async def sandbox_logs(sandbox_id: str, source: str = "all", tail: int = 200) -> Response:
    """Snapshot of the sandbox's recent logs.

    ``source`` selects which log to read:
      - ``"docker"`` — container's stdout/stderr (tini + entrypoint)
      - ``"aidream"`` — aidream FastAPI's own log file (/var/log/sandbox/aidream-server.log)
      - ``"matrx_agent"`` — matrx_agent daemon's log (/var/log/sandbox/api.log)
      - ``"entrypoint"`` — entrypoint trace (/var/log/sandbox/entrypoint.log)
      - ``"autostart"`` — aidream auto-start log (/var/log/sandbox/aidream-autostart.log)
      - ``"all"`` (default) — concatenated, each section labeled

    Returns a plain-text Response. The FE can render this as a code block
    or hand it to xterm. ``tail`` limits each source to its last N lines.
    """
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    out: list[str] = []
    valid_sources = {"docker", "aidream", "matrx_agent", "entrypoint", "autostart"}

    async def _read_inside(path: str) -> str:
        # Use the docker SDK (same one diagnostics uses) — the orchestrator
        # container is python:3.11-slim with no docker CLI installed, so
        # subprocess-based docker exec fails with FileNotFoundError.
        #
        # Gracefully handle logs that simply don't exist on this template:
        # slim/bare boxes never run aidream, so aidream-server.log and
        # aidream-autostart.log are absent. The old `tail <missing>` surfaced
        # "tail: cannot open ... No such file or directory" as if something
        # were broken. A shell `[ -f ]` guard turns that into a clean note.
        try:
            client = sandbox_manager._get_docker_client()
            container = await asyncio.to_thread(client.containers.get, sandbox_id)
            cmd = (
                f'if [ -f "{path}" ]; then tail -n {int(tail)} "{path}"; '
                f'else echo "(not present on this template — this log only exists on boxes that run that service)"; fi'
            )
            exit_code, output = await asyncio.to_thread(lambda: container.exec_run(
                ["sh", "-lc", cmd],
                stdout=True, stderr=True, demux=False,
            ))
            text = output.decode(errors="replace") if output else ""
            if exit_code != 0:
                return f"(could not read {path}: exit={exit_code}) {text.strip()}"
            return text
        except Exception as exc:
            return f"(error reading {path}: {exc})"

    sources_to_read = valid_sources if source == "all" else {source}
    if source != "all" and source not in valid_sources:
        raise HTTPException(status_code=400, detail=f"unknown source '{source}'. valid: {sorted(valid_sources | {'all'})}")

    if "docker" in sources_to_read:
        out.append("=== docker logs (container stdout/stderr) ===")
        try:
            client = sandbox_manager._get_docker_client()
            container = await asyncio.to_thread(client.containers.get, sandbox_id)
            log_bytes = await asyncio.to_thread(lambda: container.logs(tail=tail, stdout=True, stderr=True, timestamps=False))
            out.append(log_bytes.decode(errors="replace") if log_bytes else "(no log output)")
        except Exception as exc:
            out.append(f"(error fetching docker logs: {exc})")

    if "entrypoint" in sources_to_read:
        out.append("\n=== entrypoint.log ===")
        out.append(await _read_inside("/var/log/sandbox/entrypoint.log"))
    if "autostart" in sources_to_read:
        out.append("\n=== aidream-autostart.log ===")
        out.append(await _read_inside("/var/log/sandbox/aidream-autostart.log"))
    if "matrx_agent" in sources_to_read:
        out.append("\n=== matrx_agent (api.log) ===")
        out.append(await _read_inside("/var/log/sandbox/api.log"))
    if "aidream" in sources_to_read:
        out.append("\n=== aidream-server.log ===")
        out.append(await _read_inside("/var/log/sandbox/aidream-server.log"))

    return Response(content="\n".join(out), media_type="text/plain; charset=utf-8")
