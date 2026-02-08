"""Sandbox CRUD API routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from orchestrator import sandbox_manager, storage
from orchestrator.models import (
    CompletionRequest,
    CompletionResponse,
    CreateSandboxRequest,
    ErrorReport,
    ErrorResponse,
    ExecRequest,
    ExecResponse,
    HeartbeatResponse,
    SandboxListResponse,
    SandboxResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


@router.post("", response_model=SandboxResponse, status_code=201)
async def create_sandbox(req: CreateSandboxRequest):
    """Create a new sandbox for a user."""
    logger.info("Sandbox creation requested for user_id=%s", req.user_id)
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


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
