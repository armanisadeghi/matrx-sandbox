"""HMAC-SHA256 bearer tokens for browser → orchestrator direct access.

Implements the access-token contract spelled out in §2 of the React team's
"Direct Browser Access" spec (matrx-frontend/features/code/SANDBOX_DIRECT_ENDPOINTS.md).

Why hand-rolled instead of pulling PyJWT:
  - The token shape is small (8 claims), the algorithm is fixed (HS256), and
    we have stdlib + cryptography already. PyJWT would be one more wheel to
    cache in the orchestrator image.
  - Self-contained code is easier to audit for a security boundary.
  - We never need to verify tokens issued elsewhere — orchestrator is the
    sole issuer + verifier — so JWT-spec compliance beyond HS256 is moot.

Token shape::

  {header_b64}.{payload_b64}.{signature_b64}

  header  = {"alg":"HS256","typ":"JWT"}    (constant; baked into HEADER_B64)
  payload = {iss, sub, scopes, tier, iat, exp, actor, jti, single_use}
  sig     = HMAC_SHA256(secret, f"{header_b64}.{payload_b64}")

All segments are URL-safe base64 (no padding).

Single-use tokens: when ``single_use=True`` is set at issue time, the
caller is expected to call ``consume_jti(jti)`` after a successful auth
(e.g., on WebSocket upgrade) so subsequent verifications reject it. The
in-memory consumption set is good enough for v1 (tokens live ≤15 min;
orchestrator restart simply invalidates the in-flight set, which is
also a sane outcome).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

# Constant header — alg + typ are fixed for our use.
_HEADER = {"alg": "HS256", "typ": "JWT"}
HEADER_B64 = base64.urlsafe_b64encode(
    json.dumps(_HEADER, separators=(",", ":")).encode()
).rstrip(b"=").decode()

ISSUER = "matrx-sandbox-orchestrator"
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 900

# Single-use jti consumption set. In-memory; rotates on orchestrator
# restart (acceptable — tokens are 5-15 min). Move to Postgres if we
# ever need cross-process coherence.
_consumed_jtis: set[str] = set()


class TokenError(Exception):
    """Raised when a token is malformed, signed wrong, expired, or scope-mismatched."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(
    *,
    secret: str,
    sandbox_id: str,
    scopes: list[str],
    tier: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    actor: dict[str, Any] | None = None,
    single_use: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Mint a bearer token. Returns (token, payload).

    Caller is expected to enforce ``ttl_seconds`` capping before calling — we
    clamp to ``MAX_TTL_SECONDS`` here as defence-in-depth so an admin endpoint
    bug can't issue 100-year tokens.
    """
    if not secret:
        raise TokenError("token signing secret is empty")
    if not sandbox_id:
        raise TokenError("sandbox_id is required")
    if not scopes:
        raise TokenError("scopes is required")

    now = int(time.time())
    capped_ttl = max(1, min(int(ttl_seconds), MAX_TTL_SECONDS))
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": sandbox_id,
        "scopes": list(scopes),
        "tier": tier,
        "iat": now,
        "exp": now + capped_ttl,
        "actor": actor or {},
        "jti": secrets.token_urlsafe(16),
        "single_use": bool(single_use),
    }

    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{HEADER_B64}.{payload_b64}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    token = f"{HEADER_B64}.{payload_b64}.{_b64url_encode(sig)}"
    return token, payload


def verify_token(
    *,
    token: str,
    secret: str,
    expected_sandbox_id: str | None = None,
    required_scope: str | None = None,
) -> dict[str, Any]:
    """Validate a bearer token.

    Raises ``TokenError`` on any of: malformed, bad signature, expired,
    sandbox_id mismatch (if ``expected_sandbox_id`` provided), scope
    mismatch (if ``required_scope`` provided), or already-consumed JTI on
    a single-use token. Returns the payload dict on success.

    This function does NOT consume a single-use jti automatically — the
    caller decides when to commit (e.g., only after a WebSocket upgrade
    actually succeeds, not on the auth check). Use ``consume_jti(jti)``.
    """
    if not secret:
        raise TokenError("token verification secret is empty")
    if not token:
        raise TokenError("missing token")

    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("malformed token (expected three segments)")
    header_b64, payload_b64, sig_b64 = parts

    if header_b64 != HEADER_B64:
        # Hard reject any non-HS256 token — never accept alg=none.
        raise TokenError("unexpected token header")

    expected_sig = hmac.new(
        secret.encode(),
        f"{header_b64}.{payload_b64}".encode(),
        hashlib.sha256,
    ).digest()
    try:
        provided_sig = _b64url_decode(sig_b64)
    except Exception as exc:
        raise TokenError("malformed token signature") from exc
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise TokenError("bad signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:
        raise TokenError("malformed token payload") from exc

    if not isinstance(payload, dict):
        raise TokenError("token payload is not an object")

    if payload.get("iss") != ISSUER:
        raise TokenError("token issuer mismatch")

    now = int(time.time())
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < now:
        raise TokenError("token expired")

    if expected_sandbox_id is not None and payload.get("sub") != expected_sandbox_id:
        raise TokenError("token sandbox_id mismatch")

    if required_scope is not None:
        scopes = payload.get("scopes") or []
        if required_scope not in scopes:
            raise TokenError(f"missing required scope: {required_scope}")

    if payload.get("single_use"):
        jti = payload.get("jti")
        if not jti or jti in _consumed_jtis:
            raise TokenError("single-use token already consumed")

    return payload


def consume_jti(jti: str) -> None:
    """Mark a single-use jti as spent so subsequent verify_token calls reject.

    Safe to call on already-consumed jtis (idempotent set add).
    """
    if jti:
        _consumed_jtis.add(jti)


def is_jti_consumed(jti: str) -> bool:
    return jti in _consumed_jtis
