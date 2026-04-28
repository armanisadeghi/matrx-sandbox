"""HTTP client for AI Dream's cloud-files bridge.

Used by both:
- ``matrx_agent.cloud_sync.watcher`` (async, real-time uploads)
- ``matrx_agent.cli.files`` (sync, CLI commands)

Both share the same env-derived config + headers so a single set of bugs/fixes.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx


@dataclass(frozen=True)
class BridgeConfig:
    url: str  # base URL, no trailing /
    token: str
    user_id: str

    @classmethod
    def from_env(cls) -> Optional["BridgeConfig"]:
        url = os.environ.get("MATRX_AIDREAM_URL", "").rstrip("/")
        token = os.environ.get("MATRX_AIDREAM_SERVICE_TOKEN", "")
        user_id = os.environ.get("USER_ID", "")
        if not (url and token and user_id):
            return None
        return cls(url=url, token=token, user_id=user_id)

    def headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "X-Matrx-User-Id": self.user_id,
            "Accept": "application/json",
        }
        if extra:
            h.update(extra)
        return h


def report_missing(stream=sys.stderr) -> None:
    print(
        "AI Dream not configured for this sandbox.\n"
        "Need: MATRX_AIDREAM_URL, MATRX_AIDREAM_SERVICE_TOKEN, USER_ID env vars.\n"
        "Run `mtx whoami` to see what's set.",
        file=stream,
    )


PUT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
DELETE_TIMEOUT = httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0)


class AsyncBridgeClient:
    """Async client for the cloud-files bridge — used by the in-process watcher."""

    def __init__(self, cfg: BridgeConfig):
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {cfg.token}",
                "X-Matrx-User-Id": cfg.user_id,
            },
        )

    async def put_one(self, local_path: Path, remote_path: str) -> dict[str, Any]:
        """Multipart PUT a single file. Raises on non-2xx."""
        with local_path.open("rb") as fh:
            r = await self._client.put(
                f"{self._cfg.url}/api/cloud-files/put",
                files={"file": (local_path.name, fh)},
                data={"file_path": remote_path},
                timeout=PUT_TIMEOUT,
            )
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return {}

    async def delete_one(self, remote_path: str) -> None:
        """Soft-delete one file. 404 is treated as already-gone (no-op)."""
        r = await self._client.delete(
            f"{self._cfg.url}/api/cloud-files/delete",
            params={"path": remote_path},
            timeout=DELETE_TIMEOUT,
        )
        if r.status_code == 404:
            return
        r.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
