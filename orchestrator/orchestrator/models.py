"""Pydantic models for API requests and responses."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class SandboxStatus(str, Enum):
    CREATING = "creating"
    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"


class CreateSandboxRequest(BaseModel):
    user_id: str = Field(..., description="User ID this sandbox belongs to")
    config: dict = Field(default_factory=dict, description="Optional sandbox config overrides")


class SandboxResponse(BaseModel):
    sandbox_id: str
    user_id: str
    status: SandboxStatus
    container_id: str | None = None
    created_at: datetime
    hot_path: str = "/home/agent"
    cold_path: str = "/data/cold"


class SandboxListResponse(BaseModel):
    sandboxes: list[SandboxResponse]
    total: int


class ExecRequest(BaseModel):
    command: str = Field(..., description="Shell command to execute in the sandbox")
    timeout: int = Field(default=30, description="Timeout in seconds")
    user: str = Field(default="agent", description="User to run command as")


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class HealthResponse(BaseModel):
    status: str
    active_sandboxes: int
    uptime_seconds: float


class HeartbeatResponse(BaseModel):
    acknowledged: bool
    sandbox_id: str
