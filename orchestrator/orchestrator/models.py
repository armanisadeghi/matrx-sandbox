"""Pydantic models for API requests and responses."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
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


SandboxTier = Literal["ec2", "hosted"]


class ResourcesSpec(BaseModel):
    """Optional resource overrides for a sandbox container."""
    cpu: float | None = Field(default=None, ge=0.25, le=8.0, description="CPU cores")
    memory_mb: int | None = Field(default=None, ge=256, le=32768, description="RAM in MB")
    disk_mb: int | None = Field(default=None, ge=1024, description="Disk in MB (informational; not enforced today)")


class CreateSandboxRequest(BaseModel):
    user_id: str = Field(..., description="Supabase auth user UUID")
    config: dict = Field(default_factory=dict, description="Optional sandbox config overrides")
    template: str | None = Field(default=None, description="Template id (e.g. 'bare', 'node-22', 'python-3.13')")
    template_version: str | None = Field(default=None, description="Optional template version pin")
    tier: SandboxTier | None = Field(
        default=None,
        description="Advisory tier ('ec2' or 'hosted'). Each orchestrator only spawns its own tier; "
        "this field is recorded so frontends can route follow-up calls to the right orchestrator.",
    )
    resources: ResourcesSpec | None = Field(default=None, description="Optional resource overrides (hosted tier only)")
    labels: dict[str, str] | None = Field(default=None, description="Free-form tags persisted with the sandbox")
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400, description="Override default TTL")

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
    expires_at: datetime | None = Field(default=None, description="When the sandbox auto-expires")
    tier: SandboxTier | None = Field(default=None, description="Tier this sandbox runs on")
    template: str | None = None
    template_version: str | None = None
    labels: dict[str, str] | None = None


class SandboxListResponse(BaseModel):
    sandboxes: list[SandboxResponse]
    total: int


class ExecRequest(BaseModel):
    command: str = Field(..., description="Shell command to execute in the sandbox")
    timeout: int = Field(default=30, ge=1, le=600, description="Timeout in seconds (1-600)")
    user: str = Field(default="agent", description="User to run command as")
    cwd: str | None = Field(
        default=None,
        description="Working directory to run the command in. "
        "If omitted, the server uses the last known CWD for this sandbox "
        "(defaults to /home/agent on first call).",
    )
    env: dict[str, str] | None = Field(
        default=None,
        description="Additional env vars merged with the sandbox default env. "
        "Use for ad-hoc per-call values (NODE_ENV, GITHUB_TOKEN, …); persistent "
        "credentials should go through /credentials.",
    )
    stdin: str | None = Field(
        default=None,
        description="Optional stdin to feed to the command. Larger payloads bypass "
        "the command-length cap; use this instead of inline heredocs.",
    )

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
    cwd: str = Field(
        default="/home/agent",
        description="Working directory after command execution. "
        "The frontend should send this value back as 'cwd' in the next ExecRequest.",
    )


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


class ExtendRequest(BaseModel):
    ttl_seconds: int = Field(default=3600, ge=60, le=86400, description="Seconds to extend from now")


class ExtendResponse(BaseModel):
    sandbox_id: str
    ttl_seconds: int
    expires_at: datetime
    new_expires_at: datetime  # alias surfaced for clients that expect this name


class TemplateInfo(BaseModel):
    id: str
    version: str
    description: str
    image: str
    tier: SandboxTier | None = None
    languages: list[str] = Field(default_factory=list)


class TemplateListResponse(BaseModel):
    templates: list[TemplateInfo]


class RouteInfo(BaseModel):
    path: str
    methods: list[str]
    name: str | None = None
    kind: Literal["http", "websocket"] = "http"


class APISurfaceResponse(BaseModel):
    """Authoritative list of every route the orchestrator serves.

    Exists because some routes use ``@router.api_route`` with broad path catchalls
    that do not appear in the auto-generated OpenAPI schema. Frontends should rely
    on this endpoint, not ``/openapi.json``, when discovering capabilities.
    """
    service: str
    version: str
    tier: SandboxTier | None = None
    routes: list[RouteInfo]
