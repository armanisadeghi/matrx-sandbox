"""Per-sandbox daemon authentication.

The in-container matrx_agent daemon listens on 0.0.0.0:8000. When sandboxes
share a Docker network, any sandbox could otherwise reach another's daemon and
drive its fs/exec/etc. This module gates the daemon behind a per-sandbox shared
secret that the orchestrator injects (``MATRX_AGENT_TOKEN``) and forwards on
every proxied request (header ``X-Matrx-Agent-Token`` or, for WebSockets, the
``agent_token`` query param).

FAIL-OPEN by design: when ``MATRX_AGENT_TOKEN`` is not set in the container's
environment the daemon does NOT enforce anything. That keeps local dev and any
pre-rollout image working unchanged — enforcement only switches on once the
sandbox image is rebuilt with this code AND the orchestrator injects the env.
"""

from __future__ import annotations

import hmac
import os

# Read once at import — the container env is fixed for the process lifetime.
_AGENT_TOKEN = os.environ.get("MATRX_AGENT_TOKEN", "")

HEADER_NAME = "x-matrx-agent-token"
QUERY_NAME = "agent_token"


def enforcement_enabled() -> bool:
    return bool(_AGENT_TOKEN)


def token_ok(provided: str | None) -> bool:
    """True if enforcement is off, or ``provided`` matches the agent token."""
    if not _AGENT_TOKEN:
        return True
    return bool(provided) and hmac.compare_digest(provided, _AGENT_TOKEN)


def ws_token_ok(websocket) -> bool:
    """Token check for a WebSocket upgrade (header or ``?agent_token=`` query)."""
    if not _AGENT_TOKEN:
        return True
    provided = websocket.headers.get(HEADER_NAME) or websocket.query_params.get(QUERY_NAME)
    return bool(provided) and hmac.compare_digest(provided, _AGENT_TOKEN)
