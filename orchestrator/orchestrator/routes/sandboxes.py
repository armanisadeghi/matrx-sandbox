"""Sandbox CRUD API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from orchestrator import sandbox_manager, storage
from orchestrator.models import (
    CreateSandboxRequest,
    ExecRequest,
    ExecResponse,
    HeartbeatResponse,
    SandboxListResponse,
    SandboxResponse,
)

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


@router.post("", response_model=SandboxResponse, status_code=201)
async def create_sandbox(req: CreateSandboxRequest):
    """Create a new sandbox for a user."""
    # Ensure S3 storage exists for this user
    await storage.ensure_user_storage(req.user_id)

    sandbox = await sandbox_manager.create_sandbox(
        user_id=req.user_id,
        config=req.config,
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
        exit_code, stdout, stderr = await sandbox_manager.exec_in_sandbox(
            sandbox_id=sandbox_id,
            command=req.command,
            timeout=req.timeout,
            user=req.user,
        )
        return ExecResponse(exit_code=exit_code, stdout=stdout, stderr=stderr)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{sandbox_id}", status_code=204)
async def destroy_sandbox(sandbox_id: str, graceful: bool = True):
    """Destroy a sandbox."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    success = await sandbox_manager.destroy_sandbox(sandbox_id, graceful=graceful)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to destroy sandbox")


@router.post("/{sandbox_id}/heartbeat", response_model=HeartbeatResponse)
async def sandbox_heartbeat(sandbox_id: str):
    """Record a heartbeat from a sandbox."""
    ack = await sandbox_manager.heartbeat(sandbox_id)
    if not ack:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    return HeartbeatResponse(acknowledged=True, sandbox_id=sandbox_id)


@router.post("/{sandbox_id}/complete")
async def sandbox_complete(sandbox_id: str, result: dict | None = None):
    """Agent signals that its task is complete. Triggers graceful shutdown."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    await sandbox_manager.destroy_sandbox(sandbox_id, graceful=True)
    return {"status": "shutting_down", "sandbox_id": sandbox_id}


@router.post("/{sandbox_id}/error")
async def sandbox_error(sandbox_id: str, body: dict | None = None):
    """Agent signals an error. Logs the error and triggers graceful shutdown."""
    sandbox = await sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")

    # In production, log the error to a monitoring system
    error = (body or {}).get("error", "unknown error")
    import logging
    logging.getLogger(__name__).error(
        f"Sandbox {sandbox_id} reported error: {error}"
    )

    await sandbox_manager.destroy_sandbox(sandbox_id, graceful=True)
    return {"status": "shutting_down", "sandbox_id": sandbox_id, "error_received": True}
