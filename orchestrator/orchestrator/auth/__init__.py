"""Authentication primitives for the orchestrator.

- ``sandbox_token`` mints + verifies short-lived bearer tokens that browsers
  use to talk to the orchestrator directly (without going through Next.js).
- The legacy ``X-API-Key`` master path lives in
  ``orchestrator.middleware.auth`` and is unchanged.
"""

from orchestrator.auth.sandbox_token import (
    TokenError,
    consume_jti,
    is_jti_consumed,
    issue_token,
    verify_token,
)

__all__ = [
    "TokenError",
    "consume_jti",
    "is_jti_consumed",
    "issue_token",
    "verify_token",
]
