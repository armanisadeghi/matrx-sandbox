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
from matrx_api import (
    CallOptions,
    MatrxApi,
    MatrxApiError,
    Multipart,
    ServiceCredential,
)
from matrx_api.generated import files as files_api

from matrx_agent.bridge_headers import (
    REQUIRED_BRIDGE_ENV,
    identity_headers,
    missing_bridge_env,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BridgeConfig:
    url: str  # base URL, no trailing /
    token: str
    user_id: str
    organization_id: str

    @classmethod
    def from_env(cls) -> Optional["BridgeConfig"]:
        """Build the config, or None when this container is not bridge-wired.

        Fail closed on ALL FOUR variables (``REQUIRED_BRIDGE_ENV``): the
        organization is half the request context, and AI Dream refuses a bridge
        call that names no organization (400 ``organization_required``). The
        orchestrator injects ``ORGANIZATION_ID`` into every sandbox container
        (``sandbox_manager.py``), so a container missing it is a provisioning
        defect — never something this client papers over with a fallback
        organization. Callers print :func:`report_missing`, which names exactly
        which variables are absent.
        """
        if missing_bridge_env():
            return None
        return cls(
            url=os.environ["MATRX_AIDREAM_URL"].rstrip("/"),
            token=os.environ["MATRX_AIDREAM_SERVICE_TOKEN"],
            user_id=os.environ["USER_ID"],
            organization_id=os.environ["ORGANIZATION_ID"],
        )

    def headers(
        self,
        extra: Optional[dict[str, str]] = None,
        *,
        accept: Optional[str] = "application/json",
    ) -> dict[str, str]:
        """Identity headers for one bridge call — built in ONE place."""
        return identity_headers(
            token=self.token,
            user_id=self.user_id,
            organization_id=self.organization_id,
            accept=accept,
            extra=extra,
        )


def report_missing(stream=sys.stderr) -> None:
    """Say exactly which bridge variables are missing — never a vague skip.

    The headline distinguishes the two cases, because they have different
    remedies and calling both "not configured" is how a wired box got reported
    as an unwired one: nothing set at all is an unwired image, while a PARTIAL
    identity is a provisioning defect on a box that was meant to work.
    """
    missing = missing_bridge_env() or list(REQUIRED_BRIDGE_ENV)
    if len(missing) == len(REQUIRED_BRIDGE_ENV):
        headline = "AI Dream is not configured for this sandbox (no identity at all)."
    else:
        headline = (
            "This sandbox IS wired to AI Dream, but its identity is incomplete — "
            "that is a provisioning defect, not a missing feature."
        )
    print(
        headline + "\n"
        "Missing: " + ", ".join(missing) + "\n"
        "Needs all of: " + ", ".join(REQUIRED_BRIDGE_ENV) + "\n"
        "Run `mtx whoami` to see what's set.",
        file=stream,
    )


PUT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
DELETE_TIMEOUT = httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0)


def _as_status_error(exc: MatrxApiError) -> Exception:
    """The watcher, the down-direction poller and the refusal classifiers all read an ``httpx.HTTPStatusError``
    (status, ``Retry-After``, refusal body) off the error - keep that type at this one boundary. A transport
    failure keeps its own httpx type."""
    if exc.status == 0 and exc.__cause__ is not None:
        return exc.__cause__
    response = exc.response
    if response is None:  # pragma: no cover - MatrxApi always attaches the response to an HTTP failure
        return exc
    return httpx.HTTPStatusError(
        f"HTTP {exc.status} for {response.request.method} {response.request.url}",
        request=response.request,
        response=response,
    )


class AsyncBridgeClient:
    """Async client for the cloud-files bridge - used by the in-process watcher.

    Rides ``matrx_api``: the wire (service token, ``X-Matrx-User-Id``, ``X-Organization-Id``) is pinned by
    ``tests/test_bridge_client_wire_golden.py``.
    """

    def __init__(
        self,
        cfg: BridgeConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        base_url: Optional[str] = None,
    ):
        self._cfg = cfg
        self._base_url = base_url or cfg.url
        self._transport = transport
        # The cloud-files bridge is mounted under /api.
        self._api = self._make_api(route_prefix="/api")

    def _make_api(self, *, route_prefix: Optional[str]) -> MatrxApi:
        return MatrxApi(
            credentials=ServiceCredential(self._cfg.token, acting_user_id=self._cfg.user_id),
            organization_id=self._cfg.organization_id,
            base_urls={"aidream": self._base_url},
            route_prefix=route_prefix,
            transport=self._transport,
            retry_attempts=1,
        )

    @staticmethod
    def _options(timeout: httpx.Timeout) -> CallOptions:
        return CallOptions(connect_timeout=timeout.connect, total_timeout=timeout.read)

    async def put_one(
        self, local_path: Path, remote_path: str, *, expected_checksum: str
    ) -> dict[str, Any]:
        """Multipart PUT a single file. Raises on non-2xx.

        ``expected_checksum`` is REQUIRED: the sha256 this replica last saw on the
        server at ``remote_path``, or ``"absent"`` when it believes nothing is
        there. A replica never writes by path blind -- a file the cloud removed or
        moved is refused with 412 instead of coming back as a new row.
        """
        try:
            with local_path.open("rb") as fh:
                out = await files_api.put_file(
                    self._api,
                    body=Multipart(
                        data={"file_path": remote_path, "expected_checksum": expected_checksum},
                        files={"file": (local_path.name, fh)},
                    ),
                    options=self._options(PUT_TIMEOUT),
                )
        except MatrxApiError as exc:
            raise _as_status_error(exc) from exc
        return out if isinstance(out, dict) else {}

    async def delete_one(self, remote_path: str) -> None:
        """Soft-delete one file. 404 is treated as already-gone (no-op)."""
        try:
            await files_api.cloud_files_bridge_delete_file(
                self._api, query={"path": remote_path}, options=self._options(DELETE_TIMEOUT)
            )
        except MatrxApiError as exc:
            if exc.status == 404:
                return
            raise _as_status_error(exc) from exc

    async def close(self) -> None:
        await self._api.aclose()


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
    async def put_one(
        self, local_path: Path, remote_path: str, *, expected_checksum: str
    ) -> dict[str, Any]: ...
    async def delete_one(self, remote_path: str) -> None: ...
    async def close(self) -> None: ...
    # Polling fallback for the down-direction (Batch A B1). Both the bridge
    # and the local /files router serve /api/cloud-files/changes — see
    # downstream.PollingSubscriber for the consumer.
    async def list_changes(self, since_iso: str, limit: int = 1000) -> dict[str, Any]: ...

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
    try:
        # Bytes, not JSON: the contract types this op as a plain call, whose parsed body would corrupt them.
        response = await self._api.raw(
            files_api._OP_cloud_files_bridge_get_file,
            query={"path": remote_path},
            options=self._options(PUT_TIMEOUT),
        )
    except MatrxApiError as exc:
        if exc.status == 404:
            raise FileNotFoundError(remote_path) from exc
        raise _as_status_error(exc) from exc
    return response.content


async def _remote_list_files(self: AsyncBridgeClient, prefix: str = "") -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if prefix:
        params["prefix"] = prefix
    try:
        body = await files_api.cloud_files_bridge_list_files(
            self._api, query=params, options=self._options(DELETE_TIMEOUT)
        )
    except MatrxApiError as exc:
        raise _as_status_error(exc) from exc
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
    try:
        body = await files_api.list_changes(
            self._api, query={"since": since_iso, "limit": limit}, options=self._options(DELETE_TIMEOUT)
        )
    except MatrxApiError as exc:
        raise _as_status_error(exc) from exc
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


class LocalFilesClient(AsyncBridgeClient):
    """Talks to the FULL ``/files/*`` AIDream router exposed by the
    in-sandbox aidream FastAPI on ``127.0.0.1:8001``. Available only on
    ``:aidream`` images.

    Auth uses the same service token + ``X-Matrx-User-Id`` header as the
    bridge - the in-sandbox aidream is configured to accept it. No new
    credentials are introduced. The bridge operations (get/list/changes/delete) are
    inherited: it is the same ``matrx_api`` client pointed at the local server.

    Large uploads (> PRESIGNED_THRESHOLD_BYTES) take the presigned path
    (POST /files/upload/presigned → S3 PUT → POST /files/finalize-upload),
    which keeps file bytes off aidream's network entirely. Small uploads
    use a multipart PUT against the bridge endpoint (still local, still
    fast).
    """

    BASE_URL = "http://127.0.0.1:8001"

    def __init__(self, cfg: BridgeConfig, *, transport: Optional[httpx.AsyncBaseTransport] = None):
        super().__init__(cfg, transport=transport, base_url=self.BASE_URL)
        # The richer /files/* router is mounted at the server root (no /api).
        self._files_root_api = self._make_api(route_prefix=None)

    async def close(self) -> None:
        await self._files_root_api.aclose()
        await super().close()

    # ── Core ────────────────────────────────────────────────────────────────

    async def put_one(
        self, local_path: Path, remote_path: str, *, expected_checksum: str
    ) -> dict[str, Any]:
        size = local_path.stat().st_size
        if size > PRESIGNED_THRESHOLD_BYTES:
            return await self._put_presigned(local_path, remote_path, size, expected_checksum)
        return await self._put_multipart(local_path, remote_path, expected_checksum)

    # ── Extended ────────────────────────────────────────────────────────────

    async def list_versions(self, remote_path: str) -> list[dict[str, Any]]:
        file_id = await self._resolve_file_id(remote_path)
        try:
            body = await files_api.versions_list_versions(
                self._files_root_api, path={"file_id": file_id}, options=self._options(PUT_TIMEOUT)
            )
        except MatrxApiError as exc:
            raise _as_status_error(exc) from exc
        if isinstance(body, list):
            return body
        return list(body.get("versions") or [])

    async def restore_version(self, remote_path: str, version: int) -> dict[str, Any]:
        file_id = await self._resolve_file_id(remote_path)
        try:
            out = await files_api.versions_restore_version(
                self._files_root_api,
                path={"file_id": file_id, "version_number": version},
                options=self._options(PUT_TIMEOUT),
            )
        except MatrxApiError as exc:
            raise _as_status_error(exc) from exc
        return out if isinstance(out, dict) else {}

    async def diff_versions(self, remote_path: str, v1: int, v2: int) -> str:
        """Return a unified diff between two text versions.

        We fetch both versions' content via /files/{id}/versions/{n}/download
        and run difflib locally - there's no server-side diff endpoint that
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
        try:
            response = await self._files_root_api.raw(
                files_api._OP_download_version,
                path={"file_id": file_id, "version_number": version},
                options=self._options(PUT_TIMEOUT),
            )
        except MatrxApiError as exc:
            raise _as_status_error(exc) from exc
        return response.content

    async def _put_multipart(
        self, local_path: Path, remote_path: str, expected_checksum: str
    ) -> dict[str, Any]:
        try:
            with local_path.open("rb") as fh:
                out = await files_api.put_file(
                    self._api,
                    body=Multipart(
                        data={"file_path": remote_path, "expected_checksum": expected_checksum},
                        files={"file": (local_path.name, fh)},
                    ),
                    options=self._options(PUT_TIMEOUT),
                )
        except MatrxApiError as exc:
            raise _as_status_error(exc) from exc
        if isinstance(out, dict):
            return out
        return {"path": remote_path, "bytes": local_path.stat().st_size}

    async def _put_presigned(
        self, local_path: Path, remote_path: str, size: int, expected_checksum: str
    ) -> dict[str, Any]:
        """Two-step large-file upload via presigned S3 URL.

        Step 1: POST /files/upload/presigned → {url, headers, upload_id}.
                AIDream signs the URL with ITS S3 creds; sandbox stays
                credential-free.
        Step 2: PUT the file bytes directly to S3 (not an AI Matrx server: plain httpx, no Matrx identity).
        Step 3: POST /files/finalize-upload → registers the cld_files row.
        """
        content_type = "application/octet-stream"
        try:
            data = await files_api.create_presigned_upload(
                self._files_root_api,
                body={
                    "file_path": remote_path,
                    "content_type": content_type,
                    "expected_size_bytes": size,
                    "expected_checksum": expected_checksum,
                },
                options=self._options(PUT_TIMEOUT),
            )
        except MatrxApiError as exc:
            raise _as_status_error(exc) from exc
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
        try:
            finalized = await files_api.finalize_presigned_upload(
                self._files_root_api,
                body={"upload_id": upload_id, "actual_size_bytes": size},
                options=self._options(PUT_TIMEOUT),
            )
        except MatrxApiError as exc:
            raise _as_status_error(exc) from exc
        result: dict[str, Any] = dict(finalized) if isinstance(finalized, dict) else {}
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
