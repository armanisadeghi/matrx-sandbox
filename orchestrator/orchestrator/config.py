"""Orchestrator configuration — loaded from environment variables."""

from __future__ import annotations

import os
import re

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings


class HostTierUnconfiguredError(RuntimeError):
    """Raised when an operation needs an exact sandbox host tier."""


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

    # AWS / S3
    s3_bucket: str = ""
    s3_region: str = "us-east-1"

    # ── The fleet's SHAPE is not here — it is settings (USD-5) ──────────────
    # Container CPU/memory limits, session lifetime, shutdown allowance, the
    # command-length cap, the warm pool, retention and the auto-migrate gates
    # are `platform.feature_knob` rows under `infrastructure.sandbox`, read
    # through orchestrator/knobs.py (seeded by aidream migration 0636). Arman,
    # 2026-09-10: "Never an env var. Env values are only for secrets, not for
    # controlling behavior." They were MATRX_CONTAINER_CPU_LIMIT,
    # MATRX_CONTAINER_MEMORY_LIMIT, MATRX_MAX_SESSION_DURATION_SECONDS,
    # MATRX_SHUTDOWN_TIMEOUT_SECONDS, MATRX_MAX_COMMAND_LENGTH,
    # MATRX_WARM_POOL_SIZE / _TEMPLATE / _TEMPLATES, MATRX_AUTO_MIGRATE,
    # MATRX_MIGRATE_MAX_PER_PASS, MATRX_TERMINAL_RETENTION_DAYS,
    # MATRX_MIGRATE_RECENT_HEARTBEAT_SECONDS and MATRX_ENABLE_S3_MIGRATE until
    # 2026-09-11; setting any of them now does nothing. Three had NO reader at
    # all and were deleted rather than converted: MATRX_CONTAINER_DISK_LIMIT,
    # MATRX_HEALTHCHECK_INTERVAL_SECONDS, MATRX_COMMAND_TIMEOUT_SECONDS.
    #
    # What stays below names the ENVIRONMENT (secrets, endpoints, host
    # identity, paths) or this process's own launcher (debug reload, log
    # format) — never product behaviour.

    # ── Sandbox store persistence — NO DEFAULT, ON PURPOSE ──────────────────
    # This used to default to "memory". A host that never set (or misspelled)
    # MATRX_SANDBOX_STORE booted happily on an in-memory store: every
    # sandbox_instances row vanished on restart, the only signal was a
    # logger.info, and nobody found out until they went looking for a sandbox
    # that no longer existed. Same failure class as the second-database
    # incident — healthy-looking service, real data loss.
    #
    # There is now no value you can arrive at by omission. Unset or
    # unrecognized => the orchestrator REFUSES TO START (see
    # ``resolve_sandbox_store``). "memory" is an explicit local-dev/test
    # opt-in and is rejected outright on any deployed host.
    sandbox_store: str = ""         # env var: MATRX_SANDBOX_STORE (memory | postgres)
    database_url: str = ""          # env var: MATRX_DATABASE_URL

    # ── Host identity (the two declared axes, same names as aidream) ────────
    # MATRX_STAGE = production | development | local   (test auto-detected)
    # MATRX_ROLE  = app_server | worker | sandbox | sandbox_host
    # Read here only to decide how strict a config guard should be. An unknown
    # or missing stage is treated as a DEPLOYED host — guards fail CLOSED.
    stage: str = ""                 # env var: MATRX_STAGE
    role: str = ""                  # env var: MATRX_ROLE

    # Tiering — which tier this orchestrator hosts.
    # Used to reject creates that ask for the wrong tier and to surface the
    # tier in /api-surface and SandboxResponse rows.
    host_tier: str = ""             # env var: MATRX_HOST_TIER ("ec2" or "hosted")

    def resolve_host_tier(self, requested: str | None = None) -> str:
        """Resolve the requested/configured tier; never invent EC2 by omission.

        The old fallback selected ``ec2`` when both values were absent. EC2 S3
        storage and hosted Docker volumes are different resources, so this made
        successful operations target the wrong persistence system. Configure
        ``MATRX_HOST_TIER`` or pass an explicit tier when the caller truly owns
        that routing decision; there is no equivalent silent degradation.
        """
        tier = (requested or self.host_tier or "").strip().lower()
        if tier not in {"ec2", "hosted"}:
            asked = requested or "this orchestrator's storage/routing tier"
            raise HostTierUnconfiguredError(
                f"{asked!r} was requested, but no valid sandbox host tier is available. "
                "Set MATRX_HOST_TIER to exactly 'ec2' or 'hosted', or pass an explicit "
                "tier only when the caller intentionally targets that tier. EC2 is not "
                "a fallback for missing hosted-tier identity."
            )
        return tier

    # Internal development worker.  Empty means the capability is disabled.
    # The caller supplies only a safe workspace key; host paths never cross the
    # API boundary.  Access is restricted to the exact user ids declared here.
    internal_development_workspace_root: str = ""
    internal_development_user_ids: str = ""

    def internal_development_users(self) -> set[str]:
        return {
            value.strip()
            for value in self.internal_development_user_ids.split(",")
            if value.strip()
        }

    # Sentinel user_id stamped on unclaimed warm boxes (no real owner yet).
    # An identity, not a setting.
    warm_pool_sentinel_user: str = "00000000-0000-0000-0000-000000000000"

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

    # Internal/in-VPC override for resolve_internal_base() consumers. Portable
    # /agent-binding and /access-tokens responses use public_url; the receiving
    # AI Dream runtime selects its configured per-tier transport independently.
    # Private example: http://sandbox-orchestrator.internal.matrxserver.com:8000
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

    # ── Host posture + the store guard ──────────────────────────────────────

    @property
    def is_test_run(self) -> bool:
        """True inside pytest (the one place a store may be picked implicitly)."""
        return bool(os.environ.get("PYTEST_CURRENT_TEST"))

    @property
    def is_deployed_host(self) -> bool:
        """True unless this is provably a developer's local machine.

        Fails CLOSED: an unset/unknown MATRX_STAGE counts as deployed, because
        the host that forgets to declare its stage is exactly the host that
        forgets to declare its store.
        """
        if self.is_test_run:
            return False
        return self.stage.strip().lower() != "local"

    def resolve_sandbox_store(self) -> str:
        """Return "postgres" or "memory", or RAISE naming the fix.

        The whole point of this function: there is no path to an in-memory
        store that a deployed host can reach by omission or by a typo.
        """
        raw = (self.sandbox_store or "").strip().lower()

        if not raw:
            if self.is_test_run:
                return "memory"
            raise RuntimeError(
                "MATRX_SANDBOX_STORE is not set — refusing to start.\n"
                "  Accepted values: postgres | memory\n"
                "  Set MATRX_SANDBOX_STORE=postgres (plus MATRX_DATABASE_URL) on any\n"
                "  deployed orchestrator (ec2 or hosted tier). Every sandbox row lives\n"
                "  there; an in-memory store loses all of them on restart.\n"
                "  For local development only: MATRX_SANDBOX_STORE=memory together with\n"
                "  MATRX_STAGE=local."
            )

        if raw not in ("postgres", "memory"):
            raise RuntimeError(
                f"MATRX_SANDBOX_STORE={self.sandbox_store!r} is not a recognized store "
                "— refusing to start.\n"
                "  Accepted values: postgres | memory\n"
                "  A misspelling must never silently degrade to in-memory storage."
            )

        if raw == "memory" and self.is_deployed_host:
            raise RuntimeError(
                "MATRX_SANDBOX_STORE=memory is refused on a deployed host "
                "— refusing to start.\n"
                f"  MATRX_STAGE={self.stage or '(unset)'} "
                f"MATRX_HOST_TIER={self.host_tier or '(unset)'}\n"
                "  An in-memory store loses EVERY sandbox_instances row on restart.\n"
                "  Set MATRX_SANDBOX_STORE=postgres and MATRX_DATABASE_URL.\n"
                "  If this really is a developer machine, declare it: MATRX_STAGE=local."
            )

        if raw == "postgres" and not self.database_url:
            raise RuntimeError(
                "MATRX_DATABASE_URL is not set but MATRX_SANDBOX_STORE=postgres "
                "— refusing to start.\n"
                "  Set MATRX_DATABASE_URL to the platform Postgres connection string."
            )

        return raw

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
