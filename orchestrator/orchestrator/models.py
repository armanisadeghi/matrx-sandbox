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
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Optional user-visible sandbox name; sandbox_id remains the routing identity.",
    )
    organization_id: str | None = Field(
        default=None,
        description="Active organization whose permitted shared secrets are injected",
    )
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
    # TTL ceiling raised to 1 year so the "permanent default sandbox" path
    # (aidream's ensure_default_sandbox) can request a TTL the user
    # intuitively reads as "never expire while in use". Heartbeats roll
    # expires_at forward on every ping; the TTL is the idle ceiling, not a
    # hard wall-clock limit.
    ttl_seconds: int | None = Field(
        default=None, ge=60, le=31_536_000,
        description="Override default TTL (idle ceiling; heartbeats refresh it)",
    )

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        try:
            UUID(v)
        except (ValueError, AttributeError):
            raise ValueError("user_id must be a valid UUID")
        return v

    @field_validator("organization_id")
    @classmethod
    def validate_organization_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            UUID(v)
        except (ValueError, AttributeError):
            raise ValueError("organization_id must be a valid UUID")
        return v

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        if not value:
            raise ValueError("name must contain at least one non-whitespace character")
        return value


class SandboxResponse(BaseModel):
    sandbox_id: str
    user_id: str
    name: str | None = Field(
        default=None,
        description="User-visible name; clients fall back to sandbox_id when null.",
    )
    status: SandboxStatus
    container_id: str | None = None
    created_at: datetime
    updated_at: datetime | None = Field(
        default=None,
        description=(
            "Last time this row changed. The reaper's liveness sweep refreshes "
            "it every ~60s for live sandboxes, so a live-status sandbox with a "
            "stale updated_at signals the orchestrator has lost track of it. "
            "Surfaced so operators can spot stuck rows without querying Postgres."
        ),
    )
    last_heartbeat_at: datetime | None = Field(
        default=None, description="Last heartbeat received from the in-container agent (null if it never sent one)."
    )
    stopped_at: datetime | None = Field(default=None, description="When the sandbox reached a terminal status.")
    stop_reason: str | None = Field(
        default=None,
        description="Why it stopped: user_requested | expired | error | graceful_shutdown | admin.",
    )
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
    persistence_volume: str | None = Field(
        default=None,
        description=(
            "Hosted-tier per-user Docker volume backing /home/agent. Set on "
            "create; survives container destruction. EC2-tier sandboxes leave "
            "this null and use the S3 prefix model instead."
        ),
    )
    proxy_url: str | None = Field(
        default=None,
        description=(
            "Public URL the browser can hit DIRECTLY to reach the in-container "
            "matrx_agent daemon (mounted at /sandboxes/{sandbox_id}/proxy/...). "
            "Authentication is via short-lived bearer token from "
            "POST /sandboxes/{id}/access-tokens. Used by the React `code` "
            "module to route sandbox-mode AI calls (per-conversation "
            "serverOverrideUrl) without traversing Next.js. Null when the "
            "orchestrator's MATRX_PUBLIC_URL is unset."
        ),
    )


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
    # Which store backs sandbox_instances, and whether it survives a restart.
    # Surfaced so an in-memory orchestrator is visible to operators/monitors
    # without reading logs — an in-memory store on a deployed host means every
    # sandbox row disappears on the next restart.
    store_backend: str = "unknown"
    durable_storage: bool = False


class SystemInfoResponse(BaseModel):
    """Host pressure + container counts for the matrx-admin observability panel."""
    tier: SandboxTier | None = None
    uptime_seconds: float

    # Disk (root filesystem — same FS as orchestrator code + sandbox volumes)
    disk_total_bytes: int
    disk_used_bytes: int
    disk_free_bytes: int
    disk_used_pct: float

    # Memory (from /proc/meminfo, kB granularity)
    memory_total_kb: int
    memory_used_kb: int
    memory_available_kb: int
    memory_used_pct: float

    # CPU
    cpu_count: int
    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None

    # Sandbox counts — both DB and Docker views, so operators can spot drift
    sandboxes_in_db: int
    sandboxes_active: int
    sandbox_containers_total: int  # via Docker label `matrx.sandbox_id`
    sandbox_containers_running: int


class HeartbeatResponse(BaseModel):
    acknowledged: bool
    sandbox_id: str


class AccessResponse(BaseModel):
    private_key: str = Field(description="PEM-encoded Ed25519 private key (temporary, per-request)")
    username: str = Field(default="agent", description="SSH username")
    host: str = Field(description="SSH host to connect to")
    port: int = Field(description="SSH port to connect to")
    ssh_command: str = Field(description="Ready-to-use SSH command")


# ── Browser-direct access tokens (HMAC bearer tokens for /proxy/*) ─────────
# Mints short-lived tokens that browsers present on direct-to-orchestrator
# SSE / WebSocket / large-body HTTP. Next.js calls this admin-authenticated
# endpoint after verifying ownership of the sandbox; never exposed publicly.

class AgentBindingRequest(BaseModel):
    """Optional overrides for POST /sandboxes/{id}/agent-binding. Both optional;
    sensible defaults (full tool scopes + the session TTL) apply when omitted."""
    scopes: list[str] | None = None
    ttl_seconds: int | None = None


class AccessTokenRequest(BaseModel):
    scopes: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Subset of: ai, exec.run, exec.stream, pty, fs.read, fs.write, "
            "fs.watch, git, ports.read. Each scope gates a category of "
            "operation on /sandboxes/{id}/proxy/*. The 'ai' scope is the "
            "broadest; reach for the narrowest scope set the caller needs."
        ),
    )
    ttl_seconds: int | None = Field(
        default=None,
        ge=1,
        le=900,
        description=(
            "Token lifetime in seconds. Default 300 (5 min); hard cap 900 "
            "(15 min). Browsers should refresh lazily before expiry."
        ),
    )
    actor: dict | None = Field(
        default=None,
        description=(
            "Caller identity (e.g. {user_id, email}). Echoed in audit logs; "
            "Next.js fills this from the verified Supabase session before "
            "calling the orchestrator."
        ),
    )
    single_use: bool = Field(
        default=False,
        description=(
            "If true, the token's jti is invalidated on first successful use. "
            "Recommended for WebSocket upgrades where replay would otherwise "
            "be possible. False for SSE/HTTP where retry/reconnect is normal."
        ),
    )


class AccessTokenResponse(BaseModel):
    token: str = Field(description="HS256 JWT — present as Authorization: Bearer <token> on direct calls.")
    expires_at: datetime
    direct_url: str = Field(description="HTTPS base URL the browser uses for SSE / large HTTP.")
    ws_base: str = Field(description="WSS base URL the browser uses for PTY / file watcher.")
    tier: str = Field(description="ec2 | hosted — echoed for client-side sanity checking.")
    sandbox_id: str
    connection_hooks: dict | None = Field(
        default=None,
        description="Internal SessionStart hook report; null for ordinary user sandboxes.",
    )


class ExtendRequest(BaseModel):
    ttl_seconds: int = Field(default=3600, ge=60, le=31_536_000, description="Seconds to extend from now")


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
    source_sha: str
    tier: SandboxTier | None = None
    contracts: dict[str, int]
    routes: list[RouteInfo]
