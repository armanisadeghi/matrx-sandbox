"""Sandbox CRUD API routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
import httpx

from orchestrator import sandbox_manager, storage
from orchestrator.config import settings
from orchestrator.models import (
    AccessResponse,
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

