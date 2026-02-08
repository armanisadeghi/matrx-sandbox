"""API key authentication middleware.

Validates the X-API-Key header (or Authorization: Bearer <key>) against
the MATRX_API_KEY environment variable. Uses constant-time comparison
to prevent timing attacks.

If MATRX_API_KEY is empty, all requests are allowed (local dev mode).
GET /health is always exempt from authentication.
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
_EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces API key authentication on all routes
    except health checks and docs."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth if no API key is configured (local dev mode)
        if not settings.api_key:
            return await call_next(request)

        # Exempt certain paths from auth
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        # Also exempt the root info endpoint
        if request.url.path == "/":
            return await call_next(request)

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
