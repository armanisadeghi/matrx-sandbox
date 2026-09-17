"""THE ONE place the ORCHESTRATOR builds identity headers for an AI Dream call.

This is a deliberate MIRROR of the sandbox image's
``sandbox-image/sdk/matrx_agent/bridge_headers.py``. The two live in separate
Python distributions — the orchestrator runs on the host and never installs the
in-container ``matrx_agent`` SDK, and the SDK must keep installing in a sandbox
that has no orchestrator — so one cannot import the other. They therefore carry
the same header names, the same required-variable list and the same refusal,
and ``orchestrator/tests/test_bridge_header_parity.py`` fails the build if they
drift apart.

The law: a sandbox orchestrator calling AI Dream on a person's behalf forwards
BOTH halves of the request context — the actor (``X-Matrx-User-Id``) and the
organization that person is working in (``X-Organization-Id``). THE REQUEST
CONTEXT IS CARRIED, NEVER REBUILT
(``common-docs/policies/context-is-carried-never-rebuilt.md``, rule 1). AI Dream
refuses an authenticated call naming no organization (400
``organization_required``) rather than writing into whichever tenant the code
below it would have defaulted to.

Until 2026-09-17 ``sandbox_manager.create_sandbox`` hand-wrote this pair inline
for the user-secrets fetch. It happened to be correct, but it was a second
builder: the next endpoint somebody adds is the one that forgets the
organization, which is exactly the class this file removes.
"""

from __future__ import annotations

from typing import Mapping, Optional

USER_ID_HEADER = "X-Matrx-User-Id"
ORGANIZATION_ID_HEADER = "X-Organization-Id"

USER_ID_ENV = "USER_ID"
ORGANIZATION_ID_ENV = "ORGANIZATION_ID"

#: Kept byte-identical with the SDK's list (parity test).
REQUIRED_BRIDGE_ENV = (
    "MATRX_AIDREAM_URL",
    "MATRX_AIDREAM_SERVICE_TOKEN",
    USER_ID_ENV,
    ORGANIZATION_ID_ENV,
)


class BridgeIdentityMissing(RuntimeError):
    """Raised instead of sending a call that drops half the request context."""


def identity_headers(
    *,
    token: str,
    user_id: str,
    organization_id: str,
    accept: Optional[str] = "application/json",
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """The canonical header set for one orchestrator → AI Dream call.

    Raises:
        BridgeIdentityMissing: naming the exact value behind a missing header,
            rather than sending a call with half the context.
    """
    missing: list[str] = []
    if not (token or "").strip():
        missing.append("MATRX_AIDREAM_SERVICE_TOKEN")
    if not (str(user_id or "")).strip():
        missing.append(USER_ID_ENV)
    if not (str(organization_id or "")).strip():
        missing.append(ORGANIZATION_ID_ENV)
    if missing:
        raise BridgeIdentityMissing(
            "This orchestrator cannot call AI Dream: missing "
            + ", ".join(missing)
            + ". Every create request names its organization and its user; a "
            "call that cannot name both is refused here rather than landing in "
            "the wrong tenant."
        )

    headers = {
        "Authorization": f"Bearer {token}",
        USER_ID_HEADER: str(user_id),
        ORGANIZATION_ID_HEADER: str(organization_id),
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
    "REQUIRED_BRIDGE_ENV",
    "USER_ID_ENV",
    "USER_ID_HEADER",
    "identity_headers",
]
