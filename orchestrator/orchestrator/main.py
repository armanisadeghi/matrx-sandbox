"""FastAPI application — Matrx Sandbox Orchestrator."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator.config import settings
from orchestrator.routes import health, sandboxes
from orchestrator.sandbox_manager import close_docker_client
from orchestrator.storage import validate_bucket

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown hooks."""
    # Startup: validate S3 bucket is accessible (C8)
    try:
        await validate_bucket()
    except RuntimeError:
        logging.getLogger(__name__).warning(
            "S3 bucket validation failed — S3 operations may not work"
        )
    yield
    # Shutdown: close Docker client (C7)
    close_docker_client()


app = FastAPI(
    title="Matrx Sandbox Orchestrator",
    description="Manages ephemeral AI agent sandboxes",
    version="0.1.0",
    lifespan=lifespan,
)

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
