"""Agent-side client for interacting with the sandbox environment.

Provides helpers for:
- Reading sandbox metadata (sandbox ID, user ID, paths)
- Accessing hot and cold storage paths
- Signaling the orchestrator (heartbeat, completion, errors)
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from pydantic import BaseModel


class SandboxInfo(BaseModel):
    """Metadata about the current sandbox environment."""

    sandbox_id: str
    user_id: str
    hot_path: Path
    cold_path: Path


class SandboxClient:
    """Client for AI agents running inside a Matrx sandbox.

    Usage:
        from matrx_agent import SandboxClient

        client = SandboxClient()
        info = client.info()
        print(f"Running in sandbox {info.sandbox_id}")
        print(f"Hot files at: {info.hot_path}")
        print(f"Cold files at: {info.cold_path}")
    """

    def __init__(self, orchestrator_url: str | None = None) -> None:
        self._orchestrator_url = orchestrator_url or os.getenv(
            "ORCHESTRATOR_URL", "http://host.docker.internal:8000"
        )

    def info(self) -> SandboxInfo:
        """Return metadata about the current sandbox."""
        return SandboxInfo(
            sandbox_id=os.environ.get("SANDBOX_ID", "unknown"),
            user_id=os.environ.get("USER_ID", "unknown"),
            hot_path=Path(os.environ.get("HOT_PATH", "/home/agent")),
            cold_path=Path(os.environ.get("COLD_PATH", "/data/cold")),
        )

    def hot_path(self) -> Path:
        """Return the hot storage path."""
        return self.info().hot_path

    def cold_path(self) -> Path:
        """Return the cold storage path."""
        return self.info().cold_path

    async def heartbeat(self) -> bool:
        """Send a heartbeat to the orchestrator. Returns True if acknowledged."""
        info = self.info()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._orchestrator_url}/sandboxes/{info.sandbox_id}/heartbeat",
                timeout=5.0,
            )
            return resp.status_code == 200

    async def signal_complete(self, result: dict | None = None) -> None:
        """Signal the orchestrator that the agent task is complete."""
        info = self.info()
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self._orchestrator_url}/sandboxes/{info.sandbox_id}/complete",
                json=result or {},
                timeout=10.0,
            )

    async def signal_error(self, error: str) -> None:
        """Signal the orchestrator that the agent encountered an error."""
        info = self.info()
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self._orchestrator_url}/sandboxes/{info.sandbox_id}/error",
                json={"error": error},
                timeout=10.0,
            )
