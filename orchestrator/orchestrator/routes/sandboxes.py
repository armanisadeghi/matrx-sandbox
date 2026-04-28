"""Sandbox CRUD API routes."""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
import httpx

from orchestrator import sandbox_manager, storage
from orchestrator.auth import sandbox_token
from orchestrator.config import settings
from orchestrator.models import (
    AccessResponse,
    AccessTokenRequest,
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

    await storage.ensure_user_storage(req.user_id)

    effective_tier = req.tier or settings.host_tier
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


@router.get("", response_model=SandboxListResponse)
async def list_sandboxes(user_id: str | None = None):
    """List all sandboxes, optionally filtered by user."""
    sandboxes = await sandbox_manager.list_sandboxes(user_id=user_id)
    return SandboxListResponse(sandboxes=sandboxes, total=len(sandboxes))


@router.get("/{sandbox_id}", response_model=SandboxResponse)
async def get_sandbox(sandbox_id: str):
    """Get sandbox details by ID."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    return sandbox


@router.post("/{sandbox_id}/exec", response_model=ExecResponse)
async def exec_command(sandbox_id: str, req: ExecRequest):
    """Execute a command inside a running sandbox."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    try:
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
async def destroy_sandbox(sandbox_id: str, graceful: bool = True):
    """Destroy a sandbox."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    success = await sandbox_manager.destroy_sandbox(
        sandbox_id, graceful=graceful, reason="user_requested"
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to destroy sandbox")


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


@router.websocket("/{sandbox_id}/fs/watch")
async def proxy_fs_watch(sandbox_id: str, websocket: WebSocket):
    """Proxy WebSocket for file watching to the internal sandbox daemon."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        await websocket.close(code=1008, reason=f"Sandbox {sandbox_id} not found")
        return
        
    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        await websocket.close(code=1011, reason="Could not determine sandbox IP")
        return

    import websockets
    from websockets.exceptions import ConnectionClosed
    import asyncio
    
    await websocket.accept()
    
    query_string = str(websocket.query_params)
    ws_url = f"ws://{container_ip}:8000/fs/watch"
    if query_string:
        ws_url += f"?{query_string}"
        
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
        try:
            await websocket.close()
        except:
            pass


@router.api_route("/{sandbox_id}/fs/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_fs(sandbox_id: str, path: str, request: Request):
    """Proxy file system requests to the internal sandbox daemon."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
        
    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
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
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
                timeout=60.0
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Error proxying request: {exc}")

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers)
        )


@router.api_route("/{sandbox_id}/exec/stream", methods=["POST"])
async def proxy_exec_stream(sandbox_id: str, request: Request):
    """Proxy streaming exec requests to the internal sandbox daemon."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
        
    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

    url = f"http://{container_ip}:8000/exec/stream"
    
    from fastapi.responses import StreamingResponse
    
    async def stream_generator():
        async with httpx.AsyncClient() as client:
            body = await request.body()
            async with client.stream(
                method=request.method,
                url=url,
                content=body,
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
                timeout=None
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


@router.api_route("/{sandbox_id}/git/{path:path}", methods=["GET", "POST"])
async def proxy_git(sandbox_id: str, path: str, request: Request):
    """Proxy git requests to the internal sandbox daemon."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
        
    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
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
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
                timeout=120.0 # Git clones can take a while
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Error proxying request: {exc}")

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers)
        )


@router.api_route("/{sandbox_id}/credentials", methods=["POST"])
@router.api_route("/{sandbox_id}/credentials/revoke", methods=["POST"])
async def proxy_credentials(sandbox_id: str, request: Request):
    """Proxy credentials requests to the internal sandbox daemon."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
        
    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

    # The path will be either /credentials or /credentials/revoke
    # Reconstruct the path part
    path = request.url.path.split(sandbox_id)[1]
    url = f"http://{container_ip}:8000{path}"
    
    async with httpx.AsyncClient() as client:
        body = await request.body()
        params = request.query_params
        
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                params=params,
                content=body,
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
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
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        await websocket.close(code=1008, reason=f"Sandbox {sandbox_id} not found")
        return
        
    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        await websocket.close(code=1011, reason="Could not determine sandbox IP")
        return

    # Forward the WebSocket connection using websockets library
    import websockets
    from websockets.exceptions import ConnectionClosed
    import asyncio
    
    await websocket.accept()
    
    query_string = str(websocket.query_params)
    ws_url = f"ws://{container_ip}:8000/pty"
    if query_string:
        ws_url += f"?{query_string}"
        
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
        try:
            await websocket.close()
        except:
            pass

@router.api_route("/{sandbox_id}/search/{path:path}", methods=["GET", "POST"])
async def proxy_search(sandbox_id: str, path: str, request: Request):
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
        
    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
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
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
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
        
    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

    # Reconstruct the path
    path = request.url.path.split(sandbox_id)[1]
    url = f"http://{container_ip}:8000{path}"
    
    async with httpx.AsyncClient() as client:
        body = await request.body()
        params = request.query_params
        
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                params=params,
                content=body,
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
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
        
    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=500, detail="Could not determine sandbox IP")

    url = f"http://{container_ip}:8000/ports"

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")},
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

    container_ip = sandbox_manager.get_sandbox_internal_ip(sandbox_id)
    if not container_ip:
        raise HTTPException(status_code=502, detail="Sandbox container has no reachable IP")

    target_url = f"http://{container_ip}:8000/{path}"
    # Strip Authorization only when the orchestrator itself consumed it
    # (kind="scoped-bearer"). For master / scoped-header / passthrough-bearer
    # we forward Authorization unchanged so the upstream daemon can use it.
    forward_drop = _HOP_BY_HOP_FORWARD | ({"authorization"} if strip_authorization else set())
    forward_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in forward_drop
    }
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

