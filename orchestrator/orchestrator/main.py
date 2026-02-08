"""FastAPI application — Matrx Sandbox Orchestrator."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator.config import settings
from orchestrator.logging_config import setup_logging
from orchestrator.middleware.auth import APIKeyMiddleware
from orchestrator.middleware.request_logging import RequestLoggingMiddleware
from orchestrator.routes import health, sandboxes
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


app = FastAPI(
    title="Matrx Sandbox Orchestrator",
    description="Manages ephemeral AI agent sandboxes",
    version="0.1.0",
    lifespan=lifespan,
)

# API key authentication middleware
app.add_middleware(APIKeyMiddleware)
# Request/response logging middleware (runs after auth, so only logs authed requests)
app.add_middleware(RequestLoggingMiddleware)

app.include_router(sandboxes.router)
app.include_router(health.router)


@app.get("/")
async def root():
    return {
        "service": "matrx-sandbox-orchestrator",
        "version": "0.1.0",
        "docs": "/docs",
    }


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
