"""FastAPI application — Matrx Sandbox Orchestrator."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute, APIWebSocketRoute

from orchestrator.config import settings
from orchestrator.logging_config import setup_logging
from orchestrator.middleware.auth import APIKeyMiddleware
from orchestrator.middleware.request_logging import RequestLoggingMiddleware
from orchestrator.models import APISurfaceResponse, RouteInfo
from orchestrator.routes import health, sandboxes, templates, users
from orchestrator.sandbox_manager import close_docker_client, close_store
from orchestrator.storage import validate_bucket

# Configure structured logging before anything else
setup_logging()

_logger = logging.getLogger(__name__)


def _degraded_config_warnings() -> list[str]:
    """Config values whose ABSENCE quietly buys a worse orchestrator.

    Same defect shape as the old MATRX_SANDBOX_STORE default: nothing crashes,
    a capability just isn't there. These stay non-fatal (unlike the store, none
    of them destroys data that already exists) but they are never silent.
    """
    if not settings.is_deployed_host:
        return []

    out: list[str] = []

    if not settings.host_tier:
        out.append(
            "MATRX_HOST_TIER is unset — this orchestrator's reconcile/reaper sweeps "
            "are not tier-scoped and new rows carry no tier. Set it to 'ec2' or 'hosted'."
        )
    if not settings.s3_bucket:
        out.append(
            "MATRX_S3_BUCKET is unset — sandboxes get no hot/cold S3 prefixes, so "
            "nothing under /home/agent or /data/cold is backed up off the host."
        )
    if settings.host_tier == "hosted" and not (
        settings.aws_access_key_id and settings.aws_secret_access_key
    ):
        out.append(
            "MATRX_AWS_ACCESS_KEY_ID / MATRX_AWS_SECRET_ACCESS_KEY unset on the hosted "
            "tier — spawned sandboxes run hot-sync/cold-mount in LOCAL-ONLY mode: user "
            "files live only in the per-user Docker volume and are lost with it."
        )
    if not settings.resolve_aidream_service_token():
        out.append(
            "MATRX_AIDREAM_SERVICE_TOKEN unresolved (and not readable from "
            f"{settings.aidream_passthrough_env_file}) — sandboxes start with no AI Dream "
            "integration and cloud-files sync is skipped."
        )
    if not settings.access_token_secret:
        out.append(
            "MATRX_ACCESS_TOKEN_SECRET is unset — /access-tokens, /agent-binding and "
            "/proxy/* return 503, so browser-direct access to sandboxes is dead."
        )

    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown hooks."""
    # Store FIRST — before any block that swallows exceptions (the reconcile
    # blocks below catch broadly). A misconfigured MATRX_SANDBOX_STORE is a
    # refusal to start, not a warning: create_store() raises naming the
    # variable, the accepted values and the fix. Resolving it here means the
    # process dies at boot instead of at the first request.
    from orchestrator.sandbox_manager import _get_store
    from orchestrator.store import PostgresSandboxStore

    _store = _get_store()
    _logger.info(
        "Sandbox store resolved: %s (durable=%s)",
        settings.resolve_sandbox_store(),
        isinstance(_store, PostgresSandboxStore),
    )

    # Warn if running without API key authentication
    if not settings.api_key:
        if settings.is_deployed_host:
            _logger.warning(
                "\n"
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
                "!!  MATRX_API_KEY IS NOT SET ON A DEPLOYED HOST                 !!\n"
                "!!  Every orchestrator route is UNAUTHENTICATED — anyone who    !!\n"
                "!!  can reach this port can create, exec in, and destroy        !!\n"
                "!!  sandboxes. Set MATRX_API_KEY now.                           !!\n"
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
            )
        else:
            _logger.warning(
                "MATRX_API_KEY is not set — API is running WITHOUT authentication. "
                "Set MATRX_API_KEY for production use."
            )

    # Every other config value whose ABSENCE silently degrades a deployed host
    # into a lesser mode. None of these can crash the orchestrator (an
    # already-running deployment must not be bricked by a missing optional
    # value), but none of them may whisper either — same rule as the store.
    for line in _degraded_config_warnings():
        _logger.warning("DEGRADED CONFIG: %s", line)

    # Startup: validate S3 bucket is accessible (C8)
    try:
        await validate_bucket()
    except RuntimeError:
        _logger.warning(
            "S3 bucket validation failed — S3 operations may not work"
        )

    # Reconcile state from Docker — single source of truth bridge between
    # whatever store we configured (in-memory or Postgres) and the actual
    # set of running containers labeled by this orchestrator. Idempotent:
    # safe to run on every boot; corrects drift in either direction.
    # Without this, an orchestrator restart leaves the in-memory store
    # empty (so live containers go orphan in the FE) and even the Postgres
    # store goes stale if a container died while the orchestrator was
    # down. See orchestrator/reconcile.py for the full mapping.
    try:
        from orchestrator.reconcile import reconcile_from_docker
        from orchestrator.sandbox_manager import _get_store

        store = _get_store()
        summary = await reconcile_from_docker(store)
        if summary["reconciled"]:
            _logger.info(
                "Boot reconcile: rehydrated %d sandbox(es) from Docker "
                "(scanned=%d, skipped=%d, failed=%d)",
                summary["reconciled"], summary["scanned"],
                summary["skipped"], summary["failed"],
            )
    except Exception as exc:
        _logger.warning("Boot reconcile failed (continuing without it): %s", exc)

    # Liveness reconcile — the inverse sweep. ``reconcile_from_docker`` walks
    # containers that EXIST; it can never notice a row still marked live whose
    # container has vanished. That gap is what left rows stuck in 'running' for
    # days (the persistence watchdog offenders). This pass marks those rows
    # stopped and, for the containers that ARE alive, refreshes updated_at so a
    # healthy long-lived sandbox doesn't trip the watchdog's max-age SLA.
    # Tier-scoped: never touches a sibling orchestrator's rows.
    try:
        from orchestrator.reconcile import reconcile_liveness
        from orchestrator.sandbox_manager import _get_store

        live_summary = await reconcile_liveness(_get_store())
        if live_summary["stopped"] or live_summary["refreshed"]:
            _logger.info(
                "Boot liveness reconcile: stopped=%d refreshed=%d",
                len(live_summary["stopped"]), live_summary["refreshed"],
            )
    except Exception as exc:
        _logger.warning("Boot liveness reconcile failed (continuing): %s", exc)

    # Start the expiry reaper — the missing half of the sandbox lifecycle.
    # Without it, sandboxes hit ``expires_at`` and nothing happens: the
    # container runs forever, the FE blocks the user as "expired", data is
    # never flushed, and orphans pile up. The reaper sweeps on an interval,
    # gracefully tears down expired containers (running the in-container
    # final sync), preserves the per-user volume, and marks the row EXPIRED
    # so it can be resumed later. See orchestrator/reaper.py.
    import asyncio

    from orchestrator.reaper import reaper_loop

    reaper_stop = asyncio.Event()
    reaper_task = asyncio.create_task(reaper_loop(reaper_stop))

    # Warm pool — keep N pre-booted boxes ready so a launch is a fast CLAIM
    # instead of a cold create. No-op unless MATRX_WARM_POOL_SIZE > 0. See
    # orchestrator/pool.py.
    from orchestrator.pool import pool_loop

    pool_stop = asyncio.Event()
    pool_task = asyncio.create_task(pool_loop(pool_stop))

    try:
        yield
    finally:
        # Shutdown: stop background loops, then close store and Docker client.
        reaper_stop.set()
        pool_stop.set()
        for task in (reaper_task, pool_task):
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception as exc:  # pragma: no cover — defensive
                _logger.warning("Background task shutdown errored: %s", exc)
        await close_store()
        close_docker_client()


try:
    from importlib.metadata import version as _pkg_version
    SERVICE_VERSION = _pkg_version("matrx-orchestrator")
except Exception:  # pragma: no cover — fallback if package isn't installed
    SERVICE_VERSION = "0.0.0+local"


def _source_sha() -> str:
    """Return the immutable source revision stamped by the build/deployer."""
    value = os.environ.get("MATRX_SOURCE_SHA", "").strip().lower()
    if not value:
        try:
            value = (Path(__file__).resolve().parents[1] / ".source-sha").read_text().strip().lower()
        except OSError:
            value = ""
    return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else "dev"


SOURCE_SHA = _source_sha()
API_CONTRACTS = {"filesystem": 2}

app = FastAPI(
    title="Matrx Sandbox Orchestrator",
    description="Manages ephemeral AI agent sandboxes",
    version=SERVICE_VERSION,
    lifespan=lifespan,
)

# API key authentication middleware
app.add_middleware(APIKeyMiddleware)
# Request/response logging middleware (runs after auth, so only logs authed requests)
app.add_middleware(RequestLoggingMiddleware)


# CORS — required for browser-direct calls to /sandboxes/{id}/proxy/* and the
# token-issuance endpoints. Configured from MATRX_CORS_ALLOWED_ORIGINS
# (comma-separated). When unset, defaults to a sensible matrx-admin allow-list.
# We never use "*" because the bearer-token routes require credentialed-style
# CORS handling (Authorization header) and "*" + credentials is forbidden.
def _resolve_cors_origins() -> list[str]:
    raw = (settings.cors_allowed_origins or "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "https://www.aimatrx.com",
        "https://aimatrx.com",
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_resolve_cors_origins(),
    allow_origin_regex=r"^https://[a-z0-9-]+\.aimatrx\.com$|^https://[a-z0-9-]+\.vercel\.app$",
    allow_credentials=False,  # bearer-token, not cookie-based
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-API-Key",
        "X-Sandbox-Access-Token",  # scoped proxy auth (browsers — doesn't collide with Authorization)
        "X-Conversation-Id",
        "X-Instance-Id",
        "X-PTY-Cols",
        "X-PTY-Rows",
    ],
    expose_headers=["X-Sandbox-Id", "X-Tier"],
    max_age=3600,
)

app.include_router(sandboxes.router)
app.include_router(health.router)
app.include_router(templates.router)
app.include_router(users.router)


@app.get("/")
async def root():
    return {
        "service": "matrx-sandbox-orchestrator",
        "version": SERVICE_VERSION,
        "source_sha": SOURCE_SHA,
        "tier": settings.host_tier or None,
        "docs": "/docs",
        "api_surface": "/api-surface",
        "integrations": {
            # Surface so an admin can see at a glance whether the AI Dream
            # bridge + AWS S3 sync are wired up. Booleans only — never echo
            # the actual secrets.
            "aidream": {
                "configured": bool(settings.aidream_url) and bool(settings.aidream_service_token),
                "url": settings.aidream_url or None,
            },
            "s3": {
                "bucket": settings.s3_bucket or None,
                "configured": bool(settings.s3_bucket),
                "creds_passthrough": bool(settings.aws_access_key_id) and bool(settings.aws_secret_access_key),
            },
            # Visibility into the aidream-in-sandbox passthrough — which env
            # vars from this orchestrator's environ will be forwarded to
            # spawned aidream containers, and how many of those are actually
            # set. Names only; values are NEVER echoed.
            "aidream_passthrough": _aidream_passthrough_status(),
        },
    }


@app.get("/drift", tags=["meta"])
async def version_drift():
    """Image-version drift report for THIS orchestrator's tier (master-key only,
    enforced by the global APIKeyMiddleware).

    Lists every live, claimed box and whether the image it is running matches
    the current image for its template. The zero-drift system (and the Manager
    UI) reads this; it also logs loudly whenever any box is stale.
    """
    from orchestrator.sandbox_manager import _get_docker_client
    from orchestrator.versioning import drift_summary

    client = _get_docker_client()
    return await asyncio.to_thread(drift_summary, client)


@app.post("/migrate-all", tags=["meta"])
async def migrate_all():
    """Roll every drifted box on this tier onto the current image (master-key).
    Busy boxes are deferred (retry later). Safe to call repeatedly — it's the
    manual trigger for the same rolling migration the reaper runs when
    MATRX_AUTO_MIGRATE=1."""
    from orchestrator.migrate import migrate_all_drifted
    from orchestrator.sandbox_manager import _get_store

    return await migrate_all_drifted(store=_get_store())


def _aidream_passthrough_status() -> dict:
    import os
    from orchestrator.sandbox_manager import _resolve_passthrough_keys
    keys = _resolve_passthrough_keys()
    set_keys = sorted(k for k in keys if os.environ.get(k))
    missing_keys = sorted(k for k in keys if not os.environ.get(k))
    return {
        "source_file": settings.aidream_passthrough_env_file or None,
        "total_keys": len(keys),
        "configured_count": len(set_keys),
        "configured_keys": set_keys,
        "missing_count": len(missing_keys),
        "missing_keys": missing_keys,
    }


@app.get("/api-surface", response_model=APISurfaceResponse, tags=["meta"])
async def api_surface() -> APISurfaceResponse:
    """Return every route this orchestrator serves.

    The auto-generated ``/openapi.json`` omits routes registered via
    ``@router.api_route(...)`` with broad path catchalls (the ``/fs/{path}``,
    ``/git/{path}``, etc. proxies). Use this endpoint as the authoritative
    capability list for client integrations.
    """
    routes: list[RouteInfo] = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            routes.append(RouteInfo(
                path=r.path,
                methods=sorted(r.methods or []),
                name=r.name,
                kind="http",
            ))
        elif isinstance(r, APIWebSocketRoute):
            routes.append(RouteInfo(
                path=r.path,
                methods=["WS"],
                name=r.name,
                kind="websocket",
            ))
    routes.sort(key=lambda r: (r.path, r.methods))
    return APISurfaceResponse(
        service="matrx-sandbox-orchestrator",
        version=SERVICE_VERSION,
        source_sha=SOURCE_SHA,
        tier=settings.host_tier or None,
        contracts=API_CONTRACTS,
        routes=routes,
    )


def start():
    """Entry point for running the server directly."""
    import uvicorn
    uvicorn.run(
        "orchestrator.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    start()
