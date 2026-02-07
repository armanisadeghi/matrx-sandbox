"""Pydantic models for API requests and responses."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class SandboxStatus(str, Enum):
    CREATING = "creating"
    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"


_USER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,255}$")


class CreateSandboxRequest(BaseModel):
    user_id: str = Field(..., description="User ID this sandbox belongs to")
    config: dict = Field(default_factory=dict, description="Optional sandbox config overrides")

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        if not v or not _USER_ID_PATTERN.match(v):
            raise ValueError(
                "user_id must be 1-255 characters: alphanumeric, dots, dashes, underscores only"
            )
        return v


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
    timeout: int = Field(default=30, ge=1, le=600, description="Timeout in seconds (1-600)")
    user: str = Field(default="agent", description="User to run command as")

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("command must not be empty")
        if len(v) > 10000:
            raise ValueError("command exceeds maximum length of 10000 characters")
        return v


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class CompletionRequest(BaseModel):
    result: dict = Field(default_factory=dict, description="Optional result data")


class ErrorReport(BaseModel):
    error: str = Field(..., description="Error message from the agent")
    details: dict = Field(default_factory=dict, description="Optional error details")


class CompletionResponse(BaseModel):
    status: str
    sandbox_id: str


class ErrorResponse(BaseModel):
    status: str
    sandbox_id: str
    error_received: bool


class HealthResponse(BaseModel):
    status: str
    active_sandboxes: int
    uptime_seconds: float


class HeartbeatResponse(BaseModel):
    acknowledged: bool
    sandbox_id: str
