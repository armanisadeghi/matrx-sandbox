"""Pydantic models for API requests and responses."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class SandboxStatus(str, Enum):
    CREATING = "creating"
    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"
    EXPIRED = "expired"


class CreateSandboxRequest(BaseModel):
    user_id: str = Field(..., description="Supabase auth user UUID")
    config: dict = Field(default_factory=dict, description="Optional sandbox config overrides")

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        try:
            UUID(v)
        except (ValueError, AttributeError):
            raise ValueError("user_id must be a valid UUID")
        return v


class SandboxResponse(BaseModel):
    sandbox_id: str
    user_id: str
    status: SandboxStatus
    container_id: str | None = None
    created_at: datetime
    hot_path: str = "/home/agent"
    cold_path: str = "/data/cold"
    config: dict = Field(default_factory=dict)
    ssh_port: int | None = Field(default=None, description="Host port mapped to container SSH (port 22)")
    ttl_seconds: int = 7200


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


class AccessResponse(BaseModel):
    private_key: str = Field(description="PEM-encoded Ed25519 private key (temporary, per-request)")
    username: str = Field(default="agent", description="SSH username")
    host: str = Field(description="SSH host to connect to")
    port: int = Field(description="SSH port to connect to")
    ssh_command: str = Field(description="Ready-to-use SSH command")
