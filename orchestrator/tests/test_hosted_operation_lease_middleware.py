"""ASGI lifetime witnesses for the hosted operation lease middleware."""
from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.hosted_migration import HostedMigrationJournal, HostedMigrationStateError
from orchestrator.hosted_operation_lease import hosted_operation_lease
from orchestrator.middleware import hosted_operation_lease as middleware_module
from orchestrator.middleware.hosted_operation_lease import HostedOperationLeaseMiddleware


def _record(sandbox_id: str, volume: str) -> dict:
    return {
        "schema_version": 1, "sandbox_id": sandbox_id, "phase": "admitted",
        "old_id": "old", "old_name": sandbox_id, "old_image": "sha256:" + "a" * 64,
        "source_volume": volume, "source_identity": {"name": volume},
        "row_identity": {"sandbox_id": sandbox_id}, "target_name": f"{sandbox_id}-mig-op",
        "target_image": "sha256:" + "b" * 64, "operation_label": "op",
        "backup_name": "backup", "helper_image": "sha256:" + "c" * 64,
        "rollback_name": f"{sandbox_id}-old-op", "verify_timeout": 1, "stop_timeout": 1,
    }


def _exclusive_try(root: str, key: str, queue) -> None:
    try:
        with HostedMigrationJournal(Path(root)).lock(key): queue.put("acquired")
    except HostedMigrationStateError: queue.put("blocked")


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    monkeypatch.setattr(middleware_module.settings, "host_tier", "hosted")
    return HostedMigrationJournal(tmp_path)


def _wire(monkeypatch, journal, rows):
    async def store_get(sandbox_id): return rows.get(sandbox_id)
    monkeypatch.setattr(middleware_module, "_get_store", lambda: SimpleNamespace(get=store_get))
    def lease(sandbox_id, volume): return hosted_operation_lease(sandbox_id, volume, journal=journal)
    monkeypatch.setattr(middleware_module, "hosted_operation_lease", lease)


async def _call(app, scope, disconnect: asyncio.Event | None = None):
    sent = []
    async def receive():
        if disconnect:
            await disconnect.wait(); return {"type": "websocket.disconnect"}
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message): sent.append(message)
    await app(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_http_stream_holds_real_home_lock_until_stream_finishes(hosted, monkeypatch):
    rows = {"box": SimpleNamespace(persistence_volume="home")}; _wire(monkeypatch, hosted, rows)
    entered, release = asyncio.Event(), asyncio.Event()
    async def downstream(scope, receive, send):
        entered.set(); await release.wait(); await send({"type": "http.response.start", "status": 200, "headers": []}); await send({"type": "http.response.body", "body": b"ok"})
    task = asyncio.create_task(_call(HostedOperationLeaseMiddleware(downstream), {"type": "http", "path": "/sandboxes/box/exec"}))
    await entered.wait(); queue = multiprocessing.Queue(); process = multiprocessing.Process(target=_exclusive_try, args=(str(hosted.root), "volume-home", queue)); process.start(); process.join(5)
    assert queue.get(timeout=1) == "blocked"
    release.set(); assert (await task)[0]["status"] == 200
    process = multiprocessing.Process(target=_exclusive_try, args=(str(hosted.root), "volume-home", queue)); process.start(); process.join(5); assert queue.get(timeout=1) == "acquired"


@pytest.mark.asyncio
async def test_websocket_lease_is_held_until_disconnect(hosted, monkeypatch):
    rows = {"box": SimpleNamespace(persistence_volume="home")}; _wire(monkeypatch, hosted, rows)
    opened, disconnect = asyncio.Event(), asyncio.Event()
    async def downstream(scope, receive, send):
        await send({"type": "websocket.accept"}); opened.set(); await receive()
    task = asyncio.create_task(_call(HostedOperationLeaseMiddleware(downstream), {"type": "websocket", "path": "/sandboxes/box/fs/watch"}, disconnect))
    await opened.wait(); queue = multiprocessing.Queue(); process = multiprocessing.Process(target=_exclusive_try, args=(str(hosted.root), "volume-home", queue)); process.start(); process.join(5); assert queue.get(timeout=1) == "blocked"
    disconnect.set(); await task
    process = multiprocessing.Process(target=_exclusive_try, args=(str(hosted.root), "volume-home", queue)); process.start(); process.join(5); assert queue.get(timeout=1) == "acquired"


@pytest.mark.asyncio
async def test_pending_hosted_row_returns_sanitized_refusal(hosted, monkeypatch):
    rows = {"box": SimpleNamespace(persistence_volume="home")}; _wire(monkeypatch, hosted, rows)
    hosted.write(_record("other", "home"))
    sent = await _call(HostedOperationLeaseMiddleware(lambda *args: None), {"type": "http", "path": "/sandboxes/box/exec"})
    assert sent[0]["status"] == 503 and (b"retry-after", b"1") in sent[0]["headers"]


@pytest.mark.asyncio
async def test_unrelated_home_and_exception_do_not_leak_or_block(hosted, monkeypatch):
    rows = {"box-a": SimpleNamespace(persistence_volume="home-a"), "box-b": SimpleNamespace(persistence_volume="home-b")}; _wire(monkeypatch, hosted, rows)
    async def boom(scope, receive, send): raise RuntimeError("downstream")
    with pytest.raises(RuntimeError): await _call(HostedOperationLeaseMiddleware(boom), {"type": "http", "path": "/sandboxes/box-a/exec"})
    async def good(scope, receive, send): await send({"type": "http.response.start", "status": 200, "headers": []})
    assert (await _call(HostedOperationLeaseMiddleware(good), {"type": "http", "path": "/sandboxes/box-b/exec"}))[0]["status"] == 200


@pytest.mark.asyncio
async def test_claim_collection_path_reaches_create_router_without_fabricated_id_lease(hosted, monkeypatch):
    """Regression: `/sandboxes/claim` must not be interpreted as sandbox id `claim`."""
    _wire(monkeypatch, hosted, {})
    called = False
    async def create_router(scope, receive, send):
        nonlocal called; called = True
        await send({"type": "http.response.start", "status": 201, "headers": []})
    sent = await _call(HostedOperationLeaseMiddleware(create_router), {"type": "http", "path": "/sandboxes/claim"})
    assert called is True and sent[0]["status"] == 201


def test_migration_actions_bypass_shared_middleware_lease_to_acquire_exclusive_lock():
    assert middleware_module._sandbox_id({"path": "/sandboxes/box/migrate"}) is None
    assert middleware_module._sandbox_id({"path": "/sandboxes/box/refresh-platform-env"}) is None


@pytest.mark.asyncio
async def test_refresh_route_can_acquire_exclusive_home_lock_without_self_deadlock(hosted, monkeypatch):
    """Regression: middleware must not hold a shared lock around refresh migration."""
    _wire(monkeypatch, hosted, {"box": SimpleNamespace(persistence_volume="home")})
    acquired = False
    async def refresh_handler(scope, receive, send):
        nonlocal acquired
        with hosted.lock("volume-home"):
            acquired = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
    sent = await _call(HostedOperationLeaseMiddleware(refresh_handler), {"type": "http", "path": "/sandboxes/box/refresh-platform-env"})
    assert acquired is True and sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_missing_row_reaches_router_404_but_store_failure_is_sanitized_503(hosted, monkeypatch):
    """Unknown IDs retain endpoint semantics; only an unavailable authority is denied."""
    _wire(monkeypatch, hosted, {})
    async def not_found(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": []})
    sent = await _call(HostedOperationLeaseMiddleware(not_found), {"type": "http", "path": "/sandboxes/no-such-id/exec"})
    assert sent[0]["status"] == 404
    async def broken_get(_sandbox_id): raise RuntimeError("store offline")
    monkeypatch.setattr(middleware_module, "_get_store", lambda: SimpleNamespace(get=broken_get))
    sent = await _call(HostedOperationLeaseMiddleware(not_found), {"type": "http", "path": "/sandboxes/box/exec"})
    assert sent[0]["status"] == 503 and (b"retry-after", b"1") in sent[0]["headers"]


@pytest.mark.asyncio
async def test_legacy_null_volume_derives_authoritative_user_home(hosted, monkeypatch):
    user_id = "12345678-1234-1234-1234-123456789abc"
    rows = {"box": SimpleNamespace(persistence_volume=None, user_id=user_id)}
    _wire(monkeypatch, hosted, rows)
    async def good(scope, receive, send): await send({"type": "http.response.start", "status": 200, "headers": []})
    assert (await _call(HostedOperationLeaseMiddleware(good), {"type": "http", "path": "/sandboxes/box/exec"}))[0]["status"] == 200
