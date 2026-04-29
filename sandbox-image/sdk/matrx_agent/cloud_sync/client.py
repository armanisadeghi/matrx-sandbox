"""HTTP clients for AI Dream's cloud-files surface.

Two implementations behind one ``BridgeClient`` Protocol:

  - ``RemoteBridgeClient`` (alias: ``AsyncBridgeClient``) — talks to the lean
    ``/api/cloud-files/*`` bridge on the central AI Dream backend. Service
    token + ``X-Matrx-User-Id`` auth. Works on every sandbox image (``:core``,
    ``:local``, ``:aidream``). Limited surface (put/delete/list/get) — no
    versioning, no presigned uploads.
  - ``LocalFilesClient`` — talks to the FULL ``/files/*`` router on
    ``127.0.0.1:8001``, which is only present on ``:aidream`` images.
    Same service token. Adds versions / restore / diff / presigned-upload
    shapes by routing through the in-sandbox AIDream FastAPI.

Selection
---------

The watcher and CLI shouldn't care which one is in use. They call
``await select_bridge_client(cfg)`` at startup, which probes
``127.0.0.1:8001/health`` and returns whichever client is reachable.
``LocalFilesClient`` is preferred when both are available — it skips a
network hop and unlocks the richer endpoints. Falls back to
``RemoteBridgeClient`` cleanly on ``:core``/``:local`` images or if the
local FastAPI is unhealthy.

The ``BridgeClient`` ``Protocol`` defines the surface every caller can
rely on. Methods only available on the rich ``LocalFilesClient`` raise
``NotSupportedError`` from ``RemoteBridgeClient`` so callers can guard
with try/except instead of isinstance checks.

Backwards-compatibility note
----------------------------

``AsyncBridgeClient`` keeps its existing two methods (``put_one``,
``delete_one``) and is kept as an alias for ``RemoteBridgeClient`` so the
in-flight watcher code (``cloud_sync/watcher.py``) keeps working without
edits. New code (CLI extensions, large-file uploads) should call into
the Protocol via ``select_bridge_client``.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


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


# ── Public alias — clearer name for new code, kept symmetric with
# ``LocalFilesClient`` below. The watcher / CLI imports ``AsyncBridgeClient``
# today so both names resolve to the same class.
RemoteBridgeClient = AsyncBridgeClient


# ──────────────────────────────────────────────────────────────────────────
# BridgeClient Protocol + LocalFilesClient (full /files/* surface)
# ──────────────────────────────────────────────────────────────────────────


class NotSupportedError(RuntimeError):
    """Raised by RemoteBridgeClient when a caller asks for a method that
    only the rich /files/* surface implements (versions, restore, diff,
    presigned upload). Callers can try/except this to fall back gracefully.
    """


@runtime_checkable
class BridgeClient(Protocol):
    """Common surface for cloud-files clients. The watcher and CLI talk
    to whatever instance ``select_bridge_client`` returns; nothing else
    in the codebase should reach for the concrete classes directly.
    """

    # Core (both implementations)
    async def put_one(self, local_path: Path, remote_path: str) -> dict[str, Any]: ...
    async def delete_one(self, remote_path: str) -> None: ...
    async def close(self) -> None: ...

    # Extended (LocalFilesClient only — RemoteBridgeClient raises NotSupportedError)
    async def get_one(self, remote_path: str) -> bytes: ...
    async def list_files(self, prefix: str = "") -> list[dict[str, Any]]: ...
    async def list_versions(self, remote_path: str) -> list[dict[str, Any]]: ...
    async def restore_version(self, remote_path: str, version: int) -> dict[str, Any]: ...
    async def diff_versions(self, remote_path: str, v1: int, v2: int) -> str: ...


# Stub the extended methods on RemoteBridgeClient so it satisfies the
# Protocol structurally and presents a clear error to callers that try to
# use a richer feature on a :core image.

async def _remote_get_one(self: AsyncBridgeClient, remote_path: str) -> bytes:
    r = await self._client.get(
        f"{self._cfg.url}/api/cloud-files/get",
        params={"path": remote_path},
        timeout=PUT_TIMEOUT,
    )
    if r.status_code == 404:
        raise FileNotFoundError(remote_path)
    r.raise_for_status()
    return r.content


async def _remote_list_files(self: AsyncBridgeClient, prefix: str = "") -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if prefix:
        params["prefix"] = prefix
    r = await self._client.get(
        f"{self._cfg.url}/api/cloud-files/list",
        params=params,
        timeout=DELETE_TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    if isinstance(body, list):
        return body
    return list(body.get("files") or [])


async def _remote_list_changes(
    self: AsyncBridgeClient, since_iso: str, limit: int = 1000,
) -> dict[str, Any]:
    """Polling fallback for the down-direction. Returns the bridge's
    ``/api/cloud-files/changes`` envelope with ``files`` + ``next_cursor``
    + ``deletions_supported`` keys. Used by ``downstream.PollingSubscriber``.
    """
    r = await self._client.get(
        f"{self._cfg.url}/api/cloud-files/changes",
        params={"since": since_iso, "limit": limit},
        timeout=DELETE_TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    return body if isinstance(body, dict) else {"files": [], "next_cursor": since_iso}


async def _remote_unsupported(*_args: Any, **_kwargs: Any) -> Any:
    raise NotSupportedError(
        "This operation requires the :aidream sandbox image (full /files/* "
        "router). Spawn a sandbox with template='aidream' or fall back to "
        "the bridge's basic put/delete/get."
    )


# Patch the methods onto AsyncBridgeClient so it implements BridgeClient.
AsyncBridgeClient.get_one = _remote_get_one  # type: ignore[attr-defined]
AsyncBridgeClient.list_files = _remote_list_files  # type: ignore[attr-defined]
AsyncBridgeClient.list_changes = _remote_list_changes  # type: ignore[attr-defined]
AsyncBridgeClient.list_versions = _remote_unsupported  # type: ignore[attr-defined]
AsyncBridgeClient.restore_version = _remote_unsupported  # type: ignore[attr-defined]
AsyncBridgeClient.diff_versions = _remote_unsupported  # type: ignore[attr-defined]


# Files larger than this go through the presigned-upload flow when the
# rich /files/* surface is available. Below this we use multipart PUT —
# the round-trip is one fewer hop and avoids the upload_id bookkeeping.
PRESIGNED_THRESHOLD_BYTES = 10 * 1024 * 1024  # 10 MiB


class LocalFilesClient:
    """Talks to the FULL ``/files/*`` AIDream router exposed by the
    in-sandbox aidream FastAPI on ``127.0.0.1:8001``. Available only on
    ``:aidream`` images.

    Auth uses the same service token + ``X-Matrx-User-Id`` header as the
    bridge — the in-sandbox aidream is configured to accept it. No new
    credentials are introduced.

    Large uploads (> PRESIGNED_THRESHOLD_BYTES) take the presigned path
    (POST /files/upload/presigned → S3 PUT → POST /files/finalize-upload),
    which keeps file bytes off aidream's network entirely. Small uploads
    use a multipart PUT against the bridge endpoint (still local, still
    fast).
    """

    BASE_URL = "http://127.0.0.1:8001"

    def __init__(self, cfg: BridgeConfig):
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {cfg.token}",
                "X-Matrx-User-Id": cfg.user_id,
            },
        )

    # ── Core ────────────────────────────────────────────────────────────────

    async def put_one(self, local_path: Path, remote_path: str) -> dict[str, Any]:
        size = local_path.stat().st_size
        if size > PRESIGNED_THRESHOLD_BYTES:
            return await self._put_presigned(local_path, remote_path, size)
        return await self._put_multipart(local_path, remote_path)

    async def delete_one(self, remote_path: str) -> None:
        # Resolve path → file_id, then DELETE /files/{id}. The bridge
        # endpoint exists on the in-sandbox FastAPI too, so reuse it.
        r = await self._client.delete(
            f"{self.BASE_URL}/api/cloud-files/delete",
            params={"path": remote_path},
            timeout=DELETE_TIMEOUT,
        )
        if r.status_code == 404:
            return
        r.raise_for_status()

    async def get_one(self, remote_path: str) -> bytes:
        r = await self._client.get(
            f"{self.BASE_URL}/api/cloud-files/get",
            params={"path": remote_path},
            timeout=PUT_TIMEOUT,
        )
        if r.status_code == 404:
            raise FileNotFoundError(remote_path)
        r.raise_for_status()
        return r.content

    async def list_files(self, prefix: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if prefix:
            params["prefix"] = prefix
        r = await self._client.get(
            f"{self.BASE_URL}/api/cloud-files/list",
            params=params,
            timeout=DELETE_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        if isinstance(body, list):
            return body
        return list(body.get("files") or [])

    async def close(self) -> None:
        await self._client.aclose()

    # ── Extended ────────────────────────────────────────────────────────────

    async def list_versions(self, remote_path: str) -> list[dict[str, Any]]:
        file_id = await self._resolve_file_id(remote_path)
        r = await self._client.get(
            f"{self.BASE_URL}/files/{file_id}/versions",
            timeout=PUT_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        if isinstance(body, list):
            return body
        return list(body.get("versions") or [])

    async def restore_version(self, remote_path: str, version: int) -> dict[str, Any]:
        file_id = await self._resolve_file_id(remote_path)
        r = await self._client.post(
            f"{self.BASE_URL}/files/{file_id}/versions/{version}/restore",
            timeout=PUT_TIMEOUT,
        )
        r.raise_for_status()
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

    async def diff_versions(self, remote_path: str, v1: int, v2: int) -> str:
        """Return a unified diff between two text versions.

        We fetch both versions' content via /files/{id}/versions/{n}/download
        and run difflib locally — there's no server-side diff endpoint that
        returns a unified format consistently across binary/text. The agent
        gets a string ready to drop into a fenced block.
        """
        import difflib
        file_id = await self._resolve_file_id(remote_path)
        a = await self._fetch_version_bytes(file_id, v1)
        b = await self._fetch_version_bytes(file_id, v2)
        try:
            a_text = a.decode("utf-8")
            b_text = b.decode("utf-8")
        except UnicodeDecodeError:
            return f"(binary file — {len(a)} bytes vs {len(b)} bytes; cannot diff)"
        diff_lines = difflib.unified_diff(
            a_text.splitlines(keepends=True),
            b_text.splitlines(keepends=True),
            fromfile=f"{remote_path}@v{v1}",
            tofile=f"{remote_path}@v{v2}",
        )
        return "".join(diff_lines)

    # ── Internals ───────────────────────────────────────────────────────────

    async def _resolve_file_id(self, remote_path: str) -> str:
        """The bridge keys files by path; the full /files/* router keys
        by uuid. Translate via list-with-prefix and exact match.
        """
        files = await self.list_files(prefix=remote_path)
        for f in files:
            fp = f.get("file_path") or f.get("path")
            if fp == remote_path:
                fid = f.get("id") or f.get("file_id")
                if fid:
                    return str(fid)
        raise FileNotFoundError(remote_path)

    async def _fetch_version_bytes(self, file_id: str, version: int) -> bytes:
        r = await self._client.get(
            f"{self.BASE_URL}/files/{file_id}/versions/{version}/download",
            timeout=PUT_TIMEOUT,
        )
        r.raise_for_status()
        return r.content

    async def _put_multipart(self, local_path: Path, remote_path: str) -> dict[str, Any]:
        with local_path.open("rb") as fh:
            r = await self._client.put(
                f"{self.BASE_URL}/api/cloud-files/put",
                files={"file": (local_path.name, fh)},
                data={"file_path": remote_path},
                timeout=PUT_TIMEOUT,
            )
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return {"path": remote_path, "bytes": local_path.stat().st_size}

    async def _put_presigned(self, local_path: Path, remote_path: str, size: int) -> dict[str, Any]:
        """Two-step large-file upload via presigned S3 URL.

        Step 1: POST /files/upload/presigned → {url, headers, upload_id}.
                AIDream signs the URL with ITS S3 creds; sandbox stays
                credential-free.
        Step 2: PUT the file bytes directly to S3.
        Step 3: POST /files/finalize-upload → registers the cld_files row.
        """
        content_type = "application/octet-stream"
        presign = await self._client.post(
            f"{self.BASE_URL}/files/upload/presigned",
            json={
                "file_path": remote_path,
                "content_type": content_type,
                "expected_size_bytes": size,
            },
            timeout=PUT_TIMEOUT,
        )
        presign.raise_for_status()
        data = presign.json()
        upload_id = data["upload_id"]
        url = data["url"]
        headers = data.get("headers") or {}

        # Step 2: stream the file directly to S3. No proxy through aidream.
        async with httpx.AsyncClient() as s3_client:
            with local_path.open("rb") as fh:
                s3_resp = await s3_client.put(
                    url,
                    headers=headers,
                    content=fh.read(),
                    timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0),
                )
        s3_resp.raise_for_status()

        # Step 3: tell aidream the upload completed so it writes the
        # cld_files row + bumps the version.
        finalize = await self._client.post(
            f"{self.BASE_URL}/files/finalize-upload",
            json={"upload_id": upload_id, "actual_size_bytes": size},
            timeout=PUT_TIMEOUT,
        )
        finalize.raise_for_status()
        result: dict[str, Any] = finalize.json() if finalize.headers.get("content-type", "").startswith("application/json") else {}
        result.setdefault("path", remote_path)
        result.setdefault("bytes", size)
        return result


# ──────────────────────────────────────────────────────────────────────────
# Selector
# ──────────────────────────────────────────────────────────────────────────


_LOCAL_HEALTH_TIMEOUT = httpx.Timeout(connect=1.0, read=2.0, write=2.0, pool=2.0)


async def _local_files_reachable() -> bool:
    """Probe the in-sandbox aidream FastAPI for a /health 200. Cheap and
    quick — runs once at watcher startup. Returns False on any error so
    the selector falls back to the remote bridge cleanly."""
    try:
        async with httpx.AsyncClient(timeout=_LOCAL_HEALTH_TIMEOUT) as client:
            r = await client.get(f"{LocalFilesClient.BASE_URL}/health")
            return r.status_code == 200
    except Exception:
        return False


async def select_bridge_client(cfg: BridgeConfig) -> BridgeClient:
    """Pick the best available cloud-files client for this sandbox.

    Preference order:
      1. ``LocalFilesClient`` — when ``127.0.0.1:8001/health`` answers 200.
         This is the ``:aidream`` image case and is always preferred:
         richer endpoints (versions, restore, diff), no network hop to the
         public bridge, presigned uploads for files > 10 MiB.
      2. ``RemoteBridgeClient`` — the fallback. Always available as long
         as ``MATRX_AIDREAM_URL`` + ``MATRX_AIDREAM_SERVICE_TOKEN`` +
         ``USER_ID`` are set (which is what ``BridgeConfig.from_env``
         already requires).

    Logs the choice once so the watcher's startup line tells the operator
    which path is in use.
    """
    if await _local_files_reachable():
        logger.info("cloud-files: LocalFilesClient selected (in-sandbox aidream on :8001)")
        return LocalFilesClient(cfg)
    logger.info("cloud-files: RemoteBridgeClient selected (no local FastAPI)")
    return AsyncBridgeClient(cfg)


__all__ = [
    "BridgeConfig",
    "AsyncBridgeClient",
    "RemoteBridgeClient",
    "LocalFilesClient",
    "BridgeClient",
    "NotSupportedError",
    "select_bridge_client",
    "report_missing",
    "PUT_TIMEOUT",
    "DELETE_TIMEOUT",
    "PRESIGNED_THRESHOLD_BYTES",
]
