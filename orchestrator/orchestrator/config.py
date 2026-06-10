"""Orchestrator configuration — loaded from environment variables."""

from __future__ import annotations

import os
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

    # ── Warm pool ───────────────────────────────────────────────────────────
    # Keep N pre-booted, unclaimed sandboxes ready so "launch from a chat
    # window" is a fast CLAIM (adopt an already-running box) instead of a cold
    # create. The user's spec: "2 warm instances ready; when one is launched,
    # prepare another." Disabled by default (size 0) so EC2/other orchestrators
    # are unaffected; the hosted orchestrator sets MATRX_WARM_POOL_SIZE=2.
    warm_pool_size: int = 0             # env var: MATRX_WARM_POOL_SIZE
    warm_pool_template: str = "slim"    # which template to pre-warm (single-template fallback)
    # Optional per-template warm pool, e.g. "slim:1,aidream:1" (MATRX_WARM_POOL_TEMPLATES).
    # When set it takes precedence: the pool keeps EACH listed template warmed to
    # its own count, so heavy templates users actually launch (aidream) are
    # claim-fast too — not just slim. A bare name ("aidream") uses warm_pool_size.
    warm_pool_templates: str = ""       # env var: MATRX_WARM_POOL_TEMPLATES
    # Sentinel user_id stamped on unclaimed warm boxes (no real owner yet).
    warm_pool_sentinel_user: str = "00000000-0000-0000-0000-000000000000"

    # Zero-drift auto-migration. When MATRX_AUTO_MIGRATE=1 the reaper, each
    # sweep, migrates drifted boxes onto the current image (busy boxes deferred
    # to the next sweep). Default OFF so migration is opt-in per deployment until
    # an operator flips it on; per-box POST /sandboxes/{id}/migrate and
    # POST /migrate-all work regardless. migrate_max_per_pass caps swaps per
    # sweep so a big drift wave rolls gradually rather than all at once.
    auto_migrate: bool = False          # env var: MATRX_AUTO_MIGRATE
    migrate_max_per_pass: int = 2       # env var: MATRX_MIGRATE_MAX_PER_PASS
    # A box is treated as "in use" (migration deferred) if it has in-flight tool
    # calls OR a heartbeat newer than this many seconds — so a recently-active
    # session is never interrupted by a rolling migrate.
    migrate_recent_heartbeat_seconds: int = 120  # env var: MATRX_MIGRATE_RECENT_HEARTBEAT_SECONDS
    # S3-backed (EC2-tier) in-place migration. OFF by default: the swap must
    # flush the old box to S3 BEFORE booting the new one, or the new box would
    # hot-sync stale S3 and lose the final edits. The code path exists
    # (migrate._migrate_s3_ordered) but MUST be validated end-to-end against a
    # throwaway EC2/S3 sandbox before enabling. When off, S3 boxes are refused
    # (unsupported_storage), exactly as before.
    enable_s3_migrate: bool = False     # env var: MATRX_ENABLE_S3_MIGRATE

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

    # Internal/in-VPC base URL for SERVER-TO-SERVER callers (the co-located AI
    # Dream making tool calls into sandboxes). When set, /agent-binding hands
    # this private address to AI Dream so tool traffic stays on the LAN
    # (same-AZ = free + sub-ms) instead of looping back out over the public
    # internet. Browsers still get public_url via /access-tokens. Falls back to
    # public_url when unset. e.g. http://172.31.83.75:8000
    internal_url: str = ""          # env var: MATRX_INTERNAL_URL

    # Public hostname the SSH credentials returned by /sandboxes/{id}/access
    # advertise to clients. Defaults to the hostname of MATRX_PUBLIC_URL when
    # unset. Set explicitly when SSH and HTTP exit through different
    # hostnames (e.g., HTTPS via Traefik but SSH direct to the host's public
    # IP). Containers' SSH is reached on the dynamic host port mapped by
    # Docker; the firewall MUST allow that port range (Docker default
    # 32768-60999) for direct ssh / VS Code Remote-SSH to work.
    ssh_host: str = ""              # env var: MATRX_SSH_HOST

    # CORS allow-list for browser-direct calls. Comma-separated. When set,
    # only these origins receive ACAO; * is never allowed (incompatible
    # with Authorization: Bearer round-trips).
    cors_allowed_origins: str = ""  # env var: MATRX_CORS_ALLOWED_ORIGINS

    # ── aidream-in-sandbox env passthrough ──────────────────────────────────
    # When the aidream FastAPI runs INSIDE a sandbox container (template=
    # 'aidream'), it needs the same secrets the central aidream backend uses
    # — Supabase URL/keys, AI provider API keys, JWT secret, multiple DB
    # connection-pool credentials, AWS, etc.
    #
    # TWO mechanisms (combined): the orchestrator forwards every env var
    # whose NAME appears in EITHER source. Values come from os.environ
    # (read at sandbox-create time); names not present in the orchestrator's
    # environ are silently skipped.
    #
    #   1. ``aidream_passthrough_env_file`` — path to a .env-style file.
    #      Every key=... line in the file becomes a passthrough name.
    #      This makes aidream's own .env the single source of truth — when
    #      aidream adds a new required env var, dropping it into
    #      /srv/projects/aidream/.env automatically forwards it.
    #
    #   2. ``aidream_passthrough_env`` — comma-separated explicit list. Use
    #      this for env vars NOT in the file (orchestrator-host-only,
    #      operator overrides, etc.). The default below covers the names
    #      we KNOW aidream needs across DB pools, providers, and admin
    #      identities — comprehensive even if the env_file is unset.
    aidream_passthrough_env_file: str = "/srv/projects/aidream/.env"
    aidream_passthrough_env: str = (
        # AWS — region (boto3) + bucket
        "AWS_REGION,AWS_BUCKET_MODELS,"
        # Supabase — auth + JWT validation
        "SUPABASE_URL,SUPABASE_KEY,"
        "SUPABASE_DJANGO_URL,SUPABASE_DJANGO_KEY,"
        # Supabase Matrix — full DB pool + JWT secret + all connection bits
        "SUPABASE_MATRIX_URL,SUPABASE_MATRIX_KEY,SUPABASE_MATRIX_JWT_SECRET,"
        "SUPABASE_MATRIX_HOST,SUPABASE_MATRIX_PORT,"
        "SUPABASE_MATRIX_USER,SUPABASE_MATRIX_PASSWORD,"
        "SUPABASE_MATRIX_NAME,SUPABASE_MATRIX_DATABASE_NAME,"
        "SUPABASE_MATRIX_PROTOCOL,"
        # Other Supabase project pools aidream registers
        "SUPABASE_AUTOMATION_MATRIX_DB_PASSWORD,SUPABASE_SAMPLE_DB_PASSWORD,"
        # Postgres direct (matrx_dm + legacy)
        "POSTGRES_HOST,POSTGRES_DB,POSTGRES_USER,POSTGRES_PASSWORD,"
        "POSTGRES_PORT,POSTGRES_PROTOCOL,"
        "MATRX_DM_HOST,MATRX_DM_PORT,MATRX_DM_NAME,MATRX_DM_USER,"
        "MATRX_DM_PASSWORD,MATRX_DM_PROTOCOL,MATRX_DM_PROJECT_URL,"
        "MATRX_DM_PUBLISHABLE_KEY,MATRX_DM_DIRECT_CONNECTION_STRING,"
        "MATRX_DM_SECRET_KEY,"
        # AI providers — exhaustive
        "OPENAI_API_KEY,ANTHROPIC_API_KEY,ANTHROPIC_KEY,"
        "GOOGLE_API_KEY,GEMINI_API_KEY,GOOGLE_AI_STUDIO,GOOGLE_APPLICATION_CREDENTIALS,"
        "GROQ_API_KEY,CEREBRAS_API_KEY,CEREBRAS_API_KEY_PERSONAL,"
        "XAI_API_KEY,TOGETHER_API_KEY,COHERE_API_KEY,"
        "FIREWORKS_API_KEY,REPLICATE_API_TOKEN,ELEVENLABS_API_KEY,"
        "HUGGING_FACE_TOKEN_ID,HUGGINGFACE_API_KEY,"
        "MISTRAL_API_KEY,SAMBA_NOVA_API_KEY,STABILITY_API_KEY,"
        "GETIMG_API_KEY,RUNPOD_API_KEY,"
        "PLAYHT_USER_ID,PLAYHT_SECRET_KEY,"
        "CARTESIA_API_KEY,SPEECHMATICS_API_KEY,SPEECHMATICS_AUTH_TOKEN,"
        "CIVIT_API_KEY,MODEL_LABS_API_KEY,"
        # Search / scrape
        "BRAVE_SEARCH_API_KEY,BRAVE_SEARCH_API_KEY_AI,BRAVE_SEARCH_API_KEY_PRO_AI,"
        "GOOGLE_SEARCH_API_KEY,GOOGLE_SEARCH_CSE_ID,AME_GOOGLE_SEARCH_API_KEY,"
        "SERPAPI_API_KEY,RAPID_API_KEY,NEWS_API_KEY,AHREFS_MATRIX_API_KEY,"
        "PAGESPEED_INSIGHTS_API_KEY,DATA_FOR_SEO_EMAIL,DATA_FOR_SEO_PASSWORD,"
        # aidream system identifiers + admin
        "ADMIN_API_TOKEN,ADMIN_AUTH_TOKEN,ADMIN_USER_ID,SYSTEM_USER_ID,"
        "DEVELOPER_USER_ID,LOCAL_USER_ID,"
        "MATRX_ENV,LOG_LEVEL,DEBUG,ALLOWED_HOSTS,PROJECT_ID,"
        # Mongo (used in some parts of aidream)
        "MONGO_URI,MONGO_USERNAME,MONGO_PASSWORD,MONGO_API_KEY,MONGO_API_KEY_NAME,"
        # Comms / integrations
        "MAILGUN_API_KEY,"
        "TWILIO_ACCOUNT_SID,TWILIO_AUTH_TOKEN,TWILIO_PHONE_NUMBER,"
        "TWILIO_MESSAGING_SERVICE_SID,TWILIO_VERIFY_SID,TWILIO_SKIP_VALIDATION,"
        "SLACK_CLIENT_ID,SLACK_CLIENT_SECRET,SLACK_REDIRECT_URL,"
        "GITHUB_CLIENT_ID,GITHUB_CLIENT_SECRET,GITHUB_PAT,"
        "GITHUB_BOT_ACCOUNT_USERNAME,GITHUB_BOT_EMAIL,GITHUB_ORG_NAME,"
        "SHOPIFY_API_SECRET_KEY,SHOPIFY_API_ACCESS_TOKEN,SHOPIFY_APP_NAME,"
        "SHOPIFY_APP_CLIENT_ID,SHOPIFY_APP_CLIENT_SECRET,"
        "AIMATRX_OAUTH_CLIENT_ID,AIMATRX_AIDREAM_REDIRECT_URI,"
        "GOOGLE_OAUTH_CLIENT_SECRETS,FIREBASE_SERVICE_ACCOUNT,"
        # AIDream sandbox bridge token (so the in-sandbox aidream can also act
        # as an aidream-bridge consumer if its code reaches for it)
        "AIDREAM_SANDBOX_SERVICE_TOKEN,"
        # Tooling
        "TOOL_WORKSPACE_BASE,"
    )

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

    # ── Effective AI Dream credentials (with passthrough-file fallback) ──────
    # The hosted tier already mounts /srv/projects/aidream/.env via
    # aidream_passthrough_env_file. That file holds the real
    # AIDREAM_SANDBOX_SERVICE_TOKEN value (aidream's name for the shared
    # service token). Rather than force operators to ALSO set
    # MATRX_AIDREAM_SERVICE_TOKEN as a duplicate, we fall back to reading
    # the token straight out of that file when the explicit MATRX_ var is
    # unset. Result: the hosted tier "just works" with zero new config; the
    # EC2 tier (which has no such file) still needs MATRX_AIDREAM_SERVICE_TOKEN
    # set explicitly in its systemd env — the sandbox secrets_injection
    # diagnostic names exactly which is missing.

    def resolve_aidream_service_token(self) -> str:
        """Explicit MATRX_AIDREAM_SERVICE_TOKEN, else AIDREAM_SANDBOX_SERVICE_TOKEN
        read from the passthrough env file, else ''."""
        if self.aidream_service_token:
            return self.aidream_service_token
        return _read_env_file_value(
            self.aidream_passthrough_env_file, "AIDREAM_SANDBOX_SERVICE_TOKEN"
        )

    def resolve_aidream_url(self) -> str:
        """Explicit MATRX_AIDREAM_URL, else MATRX_AIDREAM_URL/AIDREAM_URL read
        from the passthrough env file, else the production default."""
        if self.aidream_url:
            return self.aidream_url.rstrip("/")
        for key in ("MATRX_AIDREAM_URL", "AIDREAM_URL", "PUBLIC_URL"):
            val = _read_env_file_value(self.aidream_passthrough_env_file, key)
            if val:
                return val.rstrip("/")
        return "https://server.app.matrxserver.com"


def _read_env_file_value(path: str, key: str) -> str:
    """Read a single VALUE from a .env-style file. Returns '' on any failure
    (missing file, missing key, unreadable). Strips surrounding quotes and an
    optional `export ` prefix — mirrors the key-parser used for passthrough."""
    path = (path or "").strip()
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                k, sep, v = line.partition("=")
                if not sep or k.strip() != key:
                    continue
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                return v
    except OSError:
        return ""
    return ""


settings = Settings()
