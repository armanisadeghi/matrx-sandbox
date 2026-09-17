"""THE ONE place a sandbox builds the identity headers for an AI Dream call.

A sandbox container is one of our servers acting for a person. Every call it
makes into AI Dream therefore carries BOTH halves of the request context —
the actor (``X-Matrx-User-Id``) and the organization that person is working
in (``X-Organization-Id``) — because THE REQUEST CONTEXT IS CARRIED, NEVER
REBUILT (``common-docs/policies/context-is-carried-never-rebuilt.md``, rule 1:
"a server-to-server call forwards both on the wire and is admitted the same
way").

Until 2026-09-17 the sandbox sent only the user. AI Dream's bridge then had no
organization to install on the request context, so every write below it fell to
a personal-organization backstop: a sandbox editing a file that belongs to a
team workspace stamped the change into the person's personal tenant, silently.
AI Dream now REFUSES a bridge call with no ``X-Organization-Id`` (HTTP 400),
so an image that predates this module fails loudly with the remedy rather than
writing into the wrong tenant.

Both values arrive as container environment (``USER_ID`` / ``ORGANIZATION_ID``),
injected by the orchestrator at create time from the organization the create
request named (``orchestrator/orchestrator/sandbox_manager.py``). A container
missing either is a provisioning defect: this module RAISES and names the
variable — it never omits a header and never substitutes a default
organization.

Every outbound AI Dream call in this image builds its headers here: the
cloud-files bridge clients (``matrx_agent.cloud_sync.client``), the ``mtx
files`` CLI, and the Browser Manager client (``matrx_tools.browser_manager``).
Shell callers get the same contract from
``sandbox-image/scripts/bridge-headers.sh``.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

USER_ID_HEADER = "X-Matrx-User-Id"
ORGANIZATION_ID_HEADER = "X-Organization-Id"

USER_ID_ENV = "USER_ID"
ORGANIZATION_ID_ENV = "ORGANIZATION_ID"

#: Every environment variable a bridge call needs, in the order a human would
#: fix them. Used by the clients' ``from_env`` refusals so one list governs.
REQUIRED_BRIDGE_ENV = (
    "MATRX_AIDREAM_URL",
    "MATRX_AIDREAM_SERVICE_TOKEN",
    USER_ID_ENV,
    ORGANIZATION_ID_ENV,
)

REMEDY = (
    "The orchestrator injects USER_ID and ORGANIZATION_ID into every sandbox "
    "container from the organization the create request named; a container "
    "missing one is a provisioning defect. Recreate the sandbox from a create "
    "request that carries its organization — never fall back to a default one."
)


class BridgeIdentityMissing(RuntimeError):
    """Raised instead of sending a bridge call that drops half the context."""


def missing_bridge_env(env: Optional[Mapping[str, str]] = None) -> list[str]:
    """Names of the required bridge environment variables that are unset."""
    source = os.environ if env is None else env
    return [name for name in REQUIRED_BRIDGE_ENV if not (source.get(name) or "").strip()]


def identity_headers(
    *,
    token: str,
    user_id: str,
    organization_id: str,
    accept: Optional[str] = "application/json",
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """The canonical header set for one sandbox → AI Dream call.

    Raises:
        BridgeIdentityMissing: naming the exact environment variable behind a
            missing value, rather than sending a call with half the context.
    """
    missing: list[str] = []
    if not (token or "").strip():
        missing.append("MATRX_AIDREAM_SERVICE_TOKEN")
    if not (user_id or "").strip():
        missing.append(USER_ID_ENV)
    if not (organization_id or "").strip():
        missing.append(ORGANIZATION_ID_ENV)
    if missing:
        raise BridgeIdentityMissing(
            "This sandbox cannot call AI Dream: missing "
            + ", ".join(missing)
            + ". "
            + REMEDY
        )

    headers = {
        "Authorization": f"Bearer {token}",
        USER_ID_HEADER: user_id,
        ORGANIZATION_ID_HEADER: organization_id,
    }
    if accept:
        headers["Accept"] = accept
    if extra:
        headers.update(extra)
    return headers


__all__ = [
    "BridgeIdentityMissing",
    "ORGANIZATION_ID_ENV",
    "ORGANIZATION_ID_HEADER",
    "REMEDY",
    "REQUIRED_BRIDGE_ENV",
    "USER_ID_ENV",
    "USER_ID_HEADER",
    "identity_headers",
    "missing_bridge_env",
]
