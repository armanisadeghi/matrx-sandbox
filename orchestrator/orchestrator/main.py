"""FastAPI application — Matrx Sandbox Orchestrator."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
    yield
    # Shutdown: close store and Docker client
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
