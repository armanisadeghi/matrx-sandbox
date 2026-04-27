"""Orchestrator configuration — loaded from environment variables."""

from __future__ import annotations

import re

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings, loaded from environment variables.

    All settings are prefixed with MATRX_ (e.g., MATRX_S3_BUCKET).
    """

    # API
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    log_level: str = "INFO"
    log_format: str = "json"     # env var: MATRX_LOG_FORMAT (json or text)

    # Authentication
    api_key: str = ""            # env var: MATRX_API_KEY
    api_key_header: str = "X-API-Key"  # env var: MATRX_API_KEY_HEADER

    # Docker
    sandbox_image: str = "matrx-sandbox:latest"
    docker_network: str = "bridge"
    container_cpu_limit: float = 2.0        # CPU cores
    container_memory_limit: str = "4g"
    container_disk_limit: str = "20g"

    # AWS / S3
    s3_bucket: str = ""
    s3_region: str = "us-east-1"

    # Sandbox defaults
    max_session_duration_seconds: int = 7200  # 2 hours
    shutdown_timeout_seconds: int = 30
    healthcheck_interval_seconds: int = 30
    max_command_length: int = 10000
    command_timeout_seconds: int = 300

    # Sandbox store persistence
    sandbox_store: str = "memory"   # env var: MATRX_SANDBOX_STORE (memory or postgres)
    database_url: str = ""          # env var: MATRX_DATABASE_URL

    # Tiering — which tier this orchestrator hosts.
    # Used to reject creates that ask for the wrong tier and to surface the
    # tier in /api-surface and SandboxResponse rows.
    host_tier: str = ""             # env var: MATRX_HOST_TIER ("ec2" or "hosted")

    # ── AI Dream integration ────────────────────────────────────────────────
    # Sandboxes need a way to call the AI Dream backend (cld_files,
    # conversation context, agent endpoints) on behalf of the user. Two parts:
    #   - aidream_url: where AI Dream lives (e.g. https://api.aidream.ai)
    #   - aidream_service_token: a service-level token the orchestrator hands
    #     each spawned sandbox; the sandbox uses it to authenticate as the
    #     specific user via a `X-Matrx-User-Id` header. AI Dream verifies the
    #     service token, then trusts the user_id header (this orchestrator is
    #     the only thing that knows the service token).
    # When unset, sandboxes start with no AI Dream integration and cloud-files
    # sync is skipped.
    aidream_url: str = ""           # env var: MATRX_AIDREAM_URL
    aidream_service_token: str = "" # env var: MATRX_AIDREAM_SERVICE_TOKEN

    # ── Browser-direct access (token-issuance, proxy, CORS) ─────────────────
    # Implements the React team's "Direct Browser Access" spec — browsers
    # bypass Next.js for SSE / WebSocket / large transfers and hit the
    # orchestrator's /sandboxes/{id}/proxy/* directly with a short-lived
    # bearer token. See orchestrator/auth/sandbox_token.py for the contract.
    #
    # Required when /access-tokens or /proxy/* routes are used; if empty,
    # those routes return 503.
    access_token_secret: str = ""   # env var: MATRX_ACCESS_TOKEN_SECRET

    # External base URL the orchestrator serves on (no trailing slash). Used
    # to build the SandboxResponse.proxy_url field and the /access-tokens
    # response's `direct_url` and `ws_base`. Hosted tier defaults to its
    # Traefik hostname; EC2 sets this via env to its public IP/DNS.
    public_url: str = ""            # env var: MATRX_PUBLIC_URL

    # CORS allow-list for browser-direct calls. Comma-separated. When set,
    # only these origins receive ACAO; * is never allowed (incompatible
    # with Authorization: Bearer round-trips).
    cors_allowed_origins: str = ""  # env var: MATRX_CORS_ALLOWED_ORIGINS

    # ── AWS credentials passthrough (hosted tier S3 sync) ───────────────────
    # On EC2-tier orchestrator: instance role provides creds, no need to set.
    # On hosted-tier orchestrator: explicit creds required to sync sandboxes
    # to S3. Both keys are passed as env vars to spawned containers; if either
    # is empty, hot-sync.sh + cold-mount.sh degrade to local-only mode.
    #
    # Accept both MATRX_AWS_* (project-prefixed, intentional) and bare AWS_*
    # (the standard AWS SDK names) so a single .env can serve both this
    # orchestrator and any other AWS-CLI-style consumer on the same host.
    aws_access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices("matrx_aws_access_key_id", "aws_access_key_id"),
    )
    aws_secret_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("matrx_aws_secret_access_key", "aws_secret_access_key"),
    )

    model_config = {"env_prefix": "MATRX_"}

    @field_validator("s3_bucket")
    @classmethod
    def validate_s3_bucket(cls, v: str) -> str:
        if not v:
            # Allow empty for local dev with LocalStack
            return v
        if len(v) < 3 or len(v) > 63:
            raise ValueError("s3_bucket must be 3-63 characters")
        if not re.match(r"^[a-z0-9][a-z0-9.\-]*[a-z0-9]$", v):
            raise ValueError("s3_bucket contains invalid characters")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return v.upper()


settings = Settings()
