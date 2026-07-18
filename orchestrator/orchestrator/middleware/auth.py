"""API key authentication middleware.

Validates the X-API-Key header (or Authorization: Bearer <key>) against
the MATRX_API_KEY environment variable. Uses constant-time comparison
to prevent timing attacks.

If MATRX_API_KEY is empty, all requests are allowed (local dev mode).
Only GET /health is exempt from authentication.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# Paths that are exempt from API key authentication
_EXEMPT_PATHS = frozenset({"/health"})


def _is_proxy_path(path: str) -> bool:
    """Match /sandboxes/{any-id}/proxy/... — these have their own (bearer
    or master-key) auth applied inside the route handler so the global
    middleware should let them pass through."""
    parts = path.split("/")
    # ['', 'sandboxes', '<id>', 'proxy', ...]
    return len(parts) >= 5 and parts[1] == "sandboxes" and parts[3] == "proxy"


# Per-sandbox subpaths that form the agent's "hands in the box" tool surface.
# These accept EITHER the master key OR a sandbox-scoped token bound to the
# {id} in the path — the same trust level as /proxy/*. This is what lets the
# aidream agent's active_sandbox binding (which authenticates with a scoped
# X-Sandbox-Access-Token) actually reach /fs and /exec. Lifecycle subpaths
# (create/delete/resume/reset/extend/access/access-tokens/complete/error) are
# deliberately NOT here — they stay master-key-only.
_SCOPED_TOOL_SUBPATHS = frozenset({
    "fs", "exec", "git", "search", "processes", "ports", "pty",
})


def _scoped_tool_sandbox_id(path: str) -> str | None:
    """If ``path`` is /sandboxes/{id}/{tool-sub}/..., return {id}; else None."""
    parts = path.split("/")
    # ['', 'sandboxes', '<id>', '<sub>', ...]
    if len(parts) >= 4 and parts[1] == "sandboxes" and parts[3] in _SCOPED_TOOL_SUBPATHS:
        return parts[2]
    return None


def _required_scope_for(path: str, method: str) -> str:
    """Map a per-sandbox tool path + HTTP method to the scope a scoped token
    must carry. A token issued for a narrow scope (e.g. only ``fs.read``) must
    NOT unlock the rest of the tool surface — that was the hole where any valid
    token authorized every subpath. Master-key callers bypass this entirely.

    The scope names match those issued by ``POST /sandboxes/{id}/agent-binding``
    and ``/access-tokens``: fs.read, fs.write, fs.watch, exec.run, exec.stream,
    git, ports.read, processes.read, pty.
    """
    parts = path.split("/")
    sub = parts[3] if len(parts) >= 4 else ""
    if sub == "fs":
        if path.endswith("/fs/watch"):
            return "fs.watch"
        return "fs.read" if method in ("GET", "HEAD") else "fs.write"
    if sub == "exec":
        return "exec.stream" if path.endswith("/exec/stream") else "exec.run"
    if sub == "search":
        # Read-only ripgrep over the workspace — gated by fs.read.
        return "fs.read"
    if sub == "processes":
        return "processes.read"
    if sub == "ports":
        return "ports.read"
    if sub == "git":
        return "git"
    if sub == "pty":
        return "pty"
    # Unknown tool subpath — fail closed with a scope no token grants.
    return "__unknown__"


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces API key authentication on all routes
    except health checks, docs, and CORS preflight + the per-sandbox
    /proxy/* routes (those handle their own auth in-route)."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth if no API key is configured (local dev mode)
        if not settings.api_key:
            return await call_next(request)

        # Exempt certain paths from auth
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        # CORS preflight must pass through; the response is shaped by the
        # CORSMiddleware sitting outside this one.
        if request.method == "OPTIONS":
            return await call_next(request)

        # Per-sandbox /proxy/* uses a per-route bearer/master combined check
        # — see proxy_to_container in routes/sandboxes.py.
        if _is_proxy_path(request.url.path):
            return await call_next(request)

        # Per-sandbox TOOL routes (fs/exec/git/search/processes/ports/pty)
        # accept the master key OR a sandbox-scoped token bound to the {id} in
        # the path. This is what makes the aidream agent's active_sandbox
        # binding (scoped X-Sandbox-Access-Token → /fs, /exec) actually work.
        tool_sandbox_id = _scoped_tool_sandbox_id(request.url.path)
        if tool_sandbox_id is not None:
            provided_key = _extract_api_key(request)
            if provided_key and hmac.compare_digest(provided_key, settings.api_key):
                return await call_next(request)
            required_scope = _required_scope_for(request.url.path, request.method)
            if _verify_scoped_token(request, tool_sandbox_id, required_scope):
                return await call_next(request)
            logger.warning(
                "Rejected tool call to %s: no valid master key or scoped token "
                "with the required scope %r",
                request.url.path, required_scope,
            )
            return JSONResponse(
                status_code=401,
                content={"detail": (
                    "This route accepts the master X-API-Key or a sandbox-scoped "
                    "X-Sandbox-Access-Token issued by POST /sandboxes/{id}/access-tokens "
                    f"that carries the '{required_scope}' scope."
                )},
            )

        # Extract API key from header
        provided_key = _extract_api_key(request)

        if not provided_key:
            logger.warning(
                "Unauthenticated request to %s from %s",
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing API key. Provide X-API-Key header or Authorization: Bearer <key>."},
            )

        # Constant-time comparison to prevent timing attacks
        if not hmac.compare_digest(provided_key, settings.api_key):
            logger.warning(
                "Invalid API key for %s from %s",
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid API key."},
            )

        return await call_next(request)


def _extract_api_key(request: Request) -> str | None:
    """Extract API key from X-API-Key header or Authorization: Bearer header."""
    # Check X-API-Key header first
    api_key = request.headers.get(settings.api_key_header)
    if api_key:
        return api_key

    # Fall back to Authorization: Bearer <key>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    return None


def _verify_scoped_token(
    request: Request, sandbox_id: str, required_scope: str | None = None
) -> bool:
    """True if the request carries a valid sandbox-scoped token bound to
    ``sandbox_id`` (via X-Sandbox-Access-Token or Authorization: Bearer) that
    also carries ``required_scope``.

    Mirrors the per-route check in ``proxy_to_container``. ``required_scope`` is
    now enforced (it used to be left None, which let any valid token for the
    sandbox reach every tool subpath regardless of the scopes it was issued
    with). Pass None only for callers that intentionally accept any scope.
    """
    if not settings.access_token_secret:
        return False
    token = (
        request.headers.get("x-sandbox-access-token")
        or request.headers.get("X-Sandbox-Access-Token")
    )
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
    if not token:
        return False

    from orchestrator.auth import sandbox_token
    try:
        sandbox_token.verify_token(
            token=token,
            secret=settings.access_token_secret,
            expected_sandbox_id=sandbox_id,
            required_scope=required_scope,
        )
        return True
    except sandbox_token.TokenError:
        return False
