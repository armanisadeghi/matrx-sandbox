"""Golden: LocalFilesClient (the in-sandbox aidream on :8001) sends exactly these identity headers, methods, paths,
queries and bodies - and the presigned S3 hop carries NO Matrx identity.

Pinned against the hand-built httpx client BEFORE it moved onto ``matrx_api`` and kept green after.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from matrx_agent.cloud_sync import client as client_module
from matrx_agent.cloud_sync.client import BridgeConfig, LocalFilesClient

ORG = "3f2b8c1e-0a4d-4c55-9e0f-1a2b3c4d5e6f"
USER = "11111111-2222-4333-8444-555555555555"
FILE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
IDENTITY = {"authorization", "x-organization-id", "x-matrx-user-id"}
WANT_IDENTITY = {"authorization": "Bearer svc-token", "x-organization-id": ORG, "x-matrx-user-id": USER}


def _identity(request: httpx.Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k in IDENTITY}


def _run(responder, action):
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    async def go():
        client = LocalFilesClient(
            BridgeConfig(url="https://server.example", token="svc-token", user_id=USER, organization_id=ORG)
        )
        try:
            return await action(client)
        finally:
            await client.close()

    mp = pytest.MonkeyPatch()
    mp.setattr(httpx, "AsyncClient", factory)
    try:
        return asyncio.run(go()), seen
    finally:
        mp.undo()


def test_bridge_ops_go_to_the_local_aidream_with_identity():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get"):
            return httpx.Response(200, content=b"bytes!")
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json=[{"file_path": "a/b.md", "id": FILE_ID}])
        if request.url.path.endswith("/changes"):
            return httpx.Response(200, json={"files": [], "next_cursor": "c"})
        if request.method == "PUT":
            return httpx.Response(200, json={"ok": 1})
        return httpx.Response(204)

    async def action(c):
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "b.md"
            local.write_text("hello")
            put = await c.put_one(local, "a/b.md", expected_checksum="absent")
        await c.delete_one("a/b.md")
        return put, await c.get_one("a/b.md"), await c.list_files("a/"), await c.list_changes("2026-01-01T00:00:00Z", 5)

    (put, body, files, changes), seen = _run(respond, action)
    assert put == {"ok": 1} and body == b"bytes!" and files[0]["id"] == FILE_ID and changes["next_cursor"] == "c"
    assert [(r.method, r.url.host, r.url.port, r.url.path, dict(r.url.params)) for r in seen] == [
        ("PUT", "127.0.0.1", 8001, "/api/cloud-files/put", {}),
        ("DELETE", "127.0.0.1", 8001, "/api/cloud-files/delete", {"path": "a/b.md"}),
        ("GET", "127.0.0.1", 8001, "/api/cloud-files/get", {"path": "a/b.md"}),
        ("GET", "127.0.0.1", 8001, "/api/cloud-files/list", {"prefix": "a/"}),
        ("GET", "127.0.0.1", 8001, "/api/cloud-files/changes", {"since": "2026-01-01T00:00:00Z", "limit": "5"}),
    ]
    for r in seen:
        assert _identity(r) == WANT_IDENTITY


def test_versions_resolve_list_restore_and_diff():
    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/cloud-files/list":
            return httpx.Response(200, json=[{"file_path": "a.md", "id": FILE_ID}])
        if path.endswith("/download"):
            return httpx.Response(200, content=b"v1\n" if "/versions/1/" in path else b"v2\n")
        if path.endswith("/restore"):
            return httpx.Response(200, json={"restored": True})
        return httpx.Response(200, json=[{"version_number": 1}])

    async def action(c):
        return (await c.list_versions("a.md"), await c.restore_version("a.md", 1), await c.diff_versions("a.md", 1, 2))

    (versions, restored, diff), seen = _run(respond, action)
    assert versions == [{"version_number": 1}] and restored == {"restored": True}
    assert "-v1" in diff and "+v2" in diff
    routes = [(r.method, r.url.path) for r in seen]
    assert ("GET", f"/files/{FILE_ID}/versions") in routes
    assert ("POST", f"/files/{FILE_ID}/versions/1/restore") in routes
    assert ("GET", f"/files/{FILE_ID}/versions/1/download") in routes
    for r in seen:
        assert _identity(r) == WANT_IDENTITY


def test_presigned_upload_hops_and_s3_carries_no_matrx_identity(monkeypatch):
    monkeypatch.setattr(client_module, "PRESIGNED_THRESHOLD_BYTES", 1)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files/upload/presigned":
            return httpx.Response(200, json={"upload_id": "u1", "url": "https://s3.example/bucket/key", "headers": {"x-amz": "1"}})
        if request.url.host == "s3.example":
            return httpx.Response(200)
        return httpx.Response(200, json={"file_id": FILE_ID})

    async def action(c):
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "big.bin"
            local.write_bytes(b"0123456789")
            return await c.put_one(local, "big.bin", expected_checksum="absent")

    result, seen = _run(respond, action)
    assert result["file_id"] == FILE_ID and result["path"] == "big.bin" and result["bytes"] == 10
    presign, s3, finalize = seen
    assert (presign.method, presign.url.path) == ("POST", "/files/upload/presigned")
    assert json.loads(presign.content) == {
        "file_path": "big.bin", "content_type": "application/octet-stream", "expected_size_bytes": 10, "expected_checksum": "absent",
    }
    assert (s3.method, s3.url.host, s3.content, s3.headers["x-amz"]) == ("PUT", "s3.example", b"0123456789", "1")
    assert _identity(s3) == {}
    assert (finalize.method, finalize.url.path) == ("POST", "/files/finalize-upload")
    assert json.loads(finalize.content) == {"upload_id": "u1", "actual_size_bytes": 10}
    assert _identity(presign) == WANT_IDENTITY and _identity(finalize) == WANT_IDENTITY
