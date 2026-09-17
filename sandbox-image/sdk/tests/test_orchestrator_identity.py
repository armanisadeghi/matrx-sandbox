"""A sandbox says who it is acting for when it signals the orchestrator.

``heartbeat`` / ``signal_complete`` / ``signal_error`` used to POST with no
headers at all — the sandbox id in the path was the whole identity, so anything
that could reach the orchestrator with an id could end somebody else's session.
They now go through the SAME builder every AI Dream call uses.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from matrx_agent.client import SandboxClient


def _client(monkeypatch, **env) -> tuple[SandboxClient, list[httpx.Request]]:
    for name in ("USER_ID", "ORGANIZATION_ID", "SANDBOX_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = SandboxClient(orchestrator_url="http://orchestrator.test")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"acknowledged": True})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class _Patched(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Patched)
    return client, seen


def test_every_orchestrator_signal_carries_both_halves(monkeypatch) -> None:
    client, seen = _client(
        monkeypatch,
        USER_ID="user-123",
        ORGANIZATION_ID="org-9",
        SANDBOX_ID="sbx-abc",
    )

    async def run() -> None:
        await client.heartbeat()
        await client.signal_complete({"ok": True})
        await client.signal_error("boom")

    asyncio.run(run())

    assert len(seen) == 3
    for request in seen:
        assert request.headers["x-matrx-user-id"] == "user-123"
        assert request.headers["x-organization-id"] == "org-9"


def test_a_box_with_no_identity_sends_none_and_says_so(monkeypatch, caplog) -> None:
    """Never half an identity — the orchestrator refuses that outright — and
    never silent about why it cannot be verified."""
    client, seen = _client(monkeypatch, SANDBOX_ID="sbx-abc", USER_ID="user-123")

    async def run() -> None:
        await client.heartbeat()

    with caplog.at_level("WARNING"):
        asyncio.run(run())

    assert "x-matrx-user-id" not in seen[0].headers
    assert "x-organization-id" not in seen[0].headers
    assert "ORGANIZATION_ID" in caplog.text
