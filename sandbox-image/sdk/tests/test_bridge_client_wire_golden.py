"""Golden: the sandbox -> AI Dream cloud-files bridge sends exactly these identity headers, method, path, query and body.

Pinned against the hand-built httpx client BEFORE it moved onto ``matrx_api`` and kept green after, so the switch is
provably a no-op on the wire. X-Request-Id is the transport's own and never an identity.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from matrx_agent.cloud_sync.client import AsyncBridgeClient, BridgeConfig

ORG = "3f2b8c1e-0a4d-4c55-9e0f-1a2b3c4d5e6f"
USER = "11111111-2222-4333-8444-555555555555"
IDENTITY = {"authorization", "x-organization-id", "x-matrx-user-id"}


def _cfg() -> BridgeConfig:
    return BridgeConfig(url="https://server.example", token="svc-token", user_id=USER, organization_id=ORG)


def _identity(request: httpx.Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k in IDENTITY}


def _run(responder, action):
    """Run ``action(client)`` against a client whose transport is ``responder``; return the requests seen."""
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    async def go():
        client = AsyncBridgeClient(_cfg())
        try:
            return await action(client)
        finally:
            await client.close()

    mp = pytest.MonkeyPatch()
    mp.setattr(httpx, "AsyncClient", factory)
    try:
        result = asyncio.run(go())
    finally:
        mp.undo()
    return result, seen


def test_put_is_multipart_with_identity():
    with tempfile.TemporaryDirectory() as d:
        local = Path(d) / "notes.md"
        local.write_text("hello")
        out, seen = _run(lambda r: httpx.Response(200, json={"ok": 1}), lambda c: c.put_one(local, "dir/notes.md", expected_checksum="absent"))
    req = seen[0]
    assert out == {"ok": 1}
    assert (req.method, req.url.host, req.url.path) == ("PUT", "server.example", "/api/cloud-files/put")
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file_path"' in req.content and b"dir/notes.md" in req.content
    # Every replica write carries its precondition (never a blind by-path write).
    assert b'name="expected_checksum"' in req.content and b"absent" in req.content
    assert b'filename="notes.md"' in req.content and b"hello" in req.content
    assert _identity(req) == {"authorization": "Bearer svc-token", "x-organization-id": ORG, "x-matrx-user-id": USER}


def test_delete_get_list_changes_routes_queries_and_identity():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get"):
            return httpx.Response(200, content=b"bytes!")
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json={"files": [{"file_path": "a"}]})
        if request.url.path.endswith("/changes"):
            return httpx.Response(200, json={"files": [], "next_cursor": "c"})
        return httpx.Response(204)

    async def action(c):
        await c.delete_one("a/b.md")
        body = await c.get_one("a/b.md")
        files = await c.list_files("a/")
        changes = await c.list_changes("2026-01-01T00:00:00Z", 50)
        return body, files, changes

    (body, files, changes), seen = _run(respond, action)
    assert body == b"bytes!" and files == [{"file_path": "a"}] and changes == {"files": [], "next_cursor": "c"}
    assert [(r.method, r.url.path, dict(r.url.params)) for r in seen] == [
        ("DELETE", "/api/cloud-files/delete", {"path": "a/b.md"}),
        ("GET", "/api/cloud-files/get", {"path": "a/b.md"}),
        ("GET", "/api/cloud-files/list", {"prefix": "a/"}),
        ("GET", "/api/cloud-files/changes", {"since": "2026-01-01T00:00:00Z", "limit": "50"}),
    ]
    for r in seen:
        assert _identity(r) == {"authorization": "Bearer svc-token", "x-organization-id": ORG, "x-matrx-user-id": USER}


def test_404_semantics_and_status_errors_stay_httpx_status_errors_with_response():
    async def missing(c):
        await c.delete_one("gone")  # 404 on delete is a no-op
        with pytest.raises(FileNotFoundError):
            await c.get_one("gone")

    _run(lambda r: httpx.Response(404, json={"detail": "nope"}), missing)

    async def refused(c):
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await c.list_files()
        return excinfo.value.response.status_code, excinfo.value.response.headers.get("retry-after")

    (status, retry), _ = _run(lambda r: httpx.Response(503, headers={"Retry-After": "7"}, json={"detail": "busy"}), refused)
    assert (status, retry) == (503, "7")
