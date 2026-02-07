"""Health check routes."""

from __future__ import annotations

import time

from fastapi import APIRouter

from orchestrator import sandbox_manager
from orchestrator.models import HealthResponse

router = APIRouter(tags=["health"])

_start_time = time.time()


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check for the orchestrator service."""
    sandboxes = await sandbox_manager.list_sandboxes()
    active = [s for s in sandboxes if s.status in ("ready", "running", "starting")]
    return HealthResponse(
        status="healthy",
        active_sandboxes=len(active),
        uptime_seconds=round(time.time() - _start_time, 1),
    )
