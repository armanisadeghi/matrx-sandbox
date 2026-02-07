"""FastAPI application — Matrx Sandbox Orchestrator."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from orchestrator.config import settings
from orchestrator.routes import health, sandboxes

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="Matrx Sandbox Orchestrator",
    description="Manages ephemeral AI agent sandboxes",
    version="0.1.0",
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
