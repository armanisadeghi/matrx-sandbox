"""Agent-side client for interacting with the sandbox environment.

Provides helpers for:
- Reading sandbox metadata (sandbox ID, user ID, paths)
- Accessing hot and cold storage paths
- Signaling the orchestrator (heartbeat, completion, errors)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = 0.5  # seconds


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
        # M7: Cache sandbox info at init instead of reading env vars every call
        self._info = SandboxInfo(
            sandbox_id=os.environ.get("SANDBOX_ID", "unknown"),
            user_id=os.environ.get("USER_ID", "unknown"),
            hot_path=Path(os.environ.get("HOT_PATH", "/home/agent")),
            cold_path=Path(os.environ.get("COLD_PATH", "/data/cold")),
        )

    def info(self) -> SandboxInfo:
        """Return metadata about the current sandbox."""
        return self._info

    def hot_path(self) -> Path:
        """Return the hot storage path."""
        return self._info.hot_path

    def cold_path(self) -> Path:
        """Return the cold storage path."""
        return self._info.cold_path

    async def _request_with_retry(
        self, method: str, path: str, timeout: float = 5.0, **kwargs
    ) -> httpx.Response:
        """Make an HTTP request with retry logic (H1)."""
        url = f"{self._orchestrator_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.request(
                        method, url, timeout=timeout, **kwargs
                    )
                    return resp
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    import asyncio
                    wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(
                        "Request to %s failed (attempt %d/%d), retrying in %.1fs: %s",
                        url, attempt, MAX_RETRIES, wait, e,
                    )
                    await asyncio.sleep(wait)
            except httpx.HTTPError as e:
                logger.error("HTTP error calling %s: %s", url, e)
                raise

        logger.error("Request to %s failed after %d attempts", url, MAX_RETRIES)
        raise RuntimeError(f"Failed to reach orchestrator at {url}") from last_error

    async def heartbeat(self) -> bool:
        """Send a heartbeat to the orchestrator. Returns True if acknowledged."""
        try:
            resp = await self._request_with_retry(
                "POST",
                f"/sandboxes/{self._info.sandbox_id}/heartbeat",
                timeout=5.0,
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def signal_complete(self, result: dict | None = None) -> None:
        """Signal the orchestrator that the agent task is complete."""
        await self._request_with_retry(
            "POST",
            f"/sandboxes/{self._info.sandbox_id}/complete",
            json={"result": result or {}},
            timeout=10.0,
        )

    async def signal_error(self, error: str) -> None:
        """Signal the orchestrator that the agent encountered an error."""
        await self._request_with_retry(
            "POST",
            f"/sandboxes/{self._info.sandbox_id}/error",
            json={"error": error},
            timeout=10.0,
        )
