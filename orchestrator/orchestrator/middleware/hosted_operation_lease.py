"""ASGI-held shared hosted-volume lease for existing sandbox operations."""
from __future__ import annotations

from typing import Callable

from orchestrator.config import settings
from orchestrator.hosted_operation_lease import HostedOperationDenied, hosted_operation_lease
from orchestrator.sandbox_manager import _get_store
from orchestrator.storage_layout import user_volume_name

_COLLECTION_PATHS = frozenset({"claim"})
_EXCLUSIVE_MIGRATION_ACTIONS = frozenset({"migrate", "refresh-platform-env"})


def _sandbox_id(scope: dict) -> str | None:
    path = scope.get("path", "")
    parts = path.split("/")
    if len(parts) < 3 or parts[1] != "sandboxes" or not parts[2]:
        return None
    if parts[2] in _COLLECTION_PATHS:
        return None
    # These endpoints acquire the migration's exclusive locks themselves.
    if len(parts) >= 4 and parts[3] in _EXCLUSIVE_MIGRATION_ACTIONS:
        return None
    return parts[2]


def _authoritative_home(sandbox) -> str | None:
    volume = getattr(sandbox, "persistence_volume", None)
    if volume:
        return volume
    user_id = getattr(sandbox, "user_id", None)
    return user_volume_name(user_id) if user_id else None


class HostedOperationLeaseMiddleware:
    """Raw ASGI middleware so WS and streaming responses retain their lease."""
    def __init__(self, app: Callable):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"} or settings.host_tier != "hosted":
            await self.app(scope, receive, send); return
        sandbox_id = _sandbox_id(scope)
        if not sandbox_id:
            await self.app(scope, receive, send); return
        try:
            store = _get_store()
            sandbox = await store.get(sandbox_id)
        except Exception:
            await self._deny(scope, send); return
        # A missing row belongs to the router: preserving its normal 404 is
        # safer and more useful than fabricating a migration refusal.
        if not sandbox:
            await self.app(scope, receive, send); return
        volume = _authoritative_home(sandbox)
        if not volume:
            await self._deny(scope, send); return
        try:
            async with hosted_operation_lease(sandbox_id, volume):
                # Re-read under flock: a replaced/deleted row cannot inherit a
                # lease that was resolved before migration admission.
                fresh = await store.get(sandbox_id)
                if (
                    not fresh
                    or getattr(fresh, "user_id", None) != getattr(sandbox, "user_id", None)
                    or _authoritative_home(fresh) != volume
                ):
                    await self._deny(scope, send); return
                await self.app(scope, receive, send)
        except HostedOperationDenied:
            await self._deny(scope, send)

    async def _deny(self, scope, send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013, "reason": "sandbox migration in progress"})
            return
        body = b'{"detail":"sandbox operation temporarily unavailable"}'
        await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json"), (b"retry-after", b"1"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
