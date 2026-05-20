"""FastAPI application — Matrx Sandbox Orchestrator."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown hooks."""
    # Warn if running without API key authentication
    if not settings.api_key:
        _logger.warning(
            "MATRX_API_KEY is not set — API is running WITHOUT authentication. "
            "Set MATRX_API_KEY for production use."
        )

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

    try:
        yield
    finally:
        # Shutdown: stop the reaper, then close store and Docker client.
        reaper_stop.set()
        try:
            await asyncio.wait_for(reaper_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            reaper_task.cancel()
        except Exception as exc:  # pragma: no cover — defensive
            _logger.warning("Reaper shutdown errored: %s", exc)
        await close_store()
        close_docker_client()


try:
    from importlib.metadata import version as _pkg_version
    SERVICE_VERSION = _pkg_version("matrx-orchestrator")
except Exception:  # pragma: no cover — fallback if package isn't installed
    SERVICE_VERSION = "0.0.0+local"

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
        tier=settings.host_tier or None,
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
