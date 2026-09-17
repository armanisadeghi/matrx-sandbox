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


# ──────────────────────────────────────────────────────────────────────────
# The other direction: a sandbox calling the ORCHESTRATOR
# ──────────────────────────────────────────────────────────────────────────
#
# ``heartbeat`` / ``complete`` / ``error`` used to be identified by the sandbox
# id in the path and nothing else, so anything that could reach the
# orchestrator with a sandbox id could end somebody else's session. The SDK now
# forwards the SAME two headers through its one builder
# (``matrx_agent.client.SandboxClient``), and these routes check them against
# the sandbox's own row.
#
# An old image sends neither header. The live fleet is on that image, so
# absence is ACCEPTED and named (``identity: "unverified"`` in the answer) —
# never silently treated as a match. Anything else is refused: a mismatch is
# one box acting for another, and half an identity is a provisioning defect.

_UNVERIFIED_NOTE = (
    "This sandbox's image predates forwarded identity (2026-09-17), so the "
    "orchestrator cannot check that the caller is the sandbox it claims to be. "
    "Recreate the box on the current image to get the check."
)


class SandboxIdentityMismatch(RuntimeError):
    """Raised when a forwarded identity disagrees with the sandbox's row."""


def verify_forwarded_identity(sandbox, headers: Mapping[str, str]) -> str:
    """Check a sandbox's forwarded identity against its row.

    Returns ``"verified"`` when both headers match the row, or ``"unverified"``
    when the caller sent neither (an image that predates this contract).

    Raises:
        SandboxIdentityMismatch: on any disagreement, and on a half-sent
            identity — both name the remedy.
    """
    forwarded_user = (headers.get(USER_ID_HEADER) or "").strip()
    forwarded_org = (headers.get(ORGANIZATION_ID_HEADER) or "").strip()
    if not forwarded_user and not forwarded_org:
        return "unverified"
    if not forwarded_user or not forwarded_org:
        missing = USER_ID_HEADER if not forwarded_user else ORGANIZATION_ID_HEADER
        raise SandboxIdentityMismatch(
            f"This call named only half its identity ({missing} is missing). A "
            "sandbox forwards BOTH the acting user and the organization, from "
            "the container environment the orchestrator injected. A container "
            "missing one is a provisioning defect: recreate the sandbox from a "
            "create request that carries its organization."
        )
    row_user = (getattr(sandbox, "user_id", "") or "").strip()
    row_org = (getattr(sandbox, "organization_id", "") or "").strip()
    if forwarded_user != row_user or forwarded_org != row_org:
        raise SandboxIdentityMismatch(
            "The identity this call forwarded is not the identity this sandbox "
            "was created with, so the orchestrator will not act on it. The "
            "sandbox's own USER_ID and ORGANIZATION_ID are injected by the "
            "orchestrator at create time and must be sent unchanged; if this "
            "box was reassigned, destroy it and create a new one for the "
            "intended user and organization."
        )
    return "verified"


UNVERIFIED_IDENTITY_NOTE = _UNVERIFIED_NOTE
