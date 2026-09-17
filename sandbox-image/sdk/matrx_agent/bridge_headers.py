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

Where the values come from when the process was not started by the entrypoint:
sshd passes the container environment to nothing it launches, so `mtx` run from
an SSH session used to see no identity at all and announced "AI Dream not
configured for this sandbox" on a fully wired box. Every entrypoint therefore
publishes the same identity the daemon holds to ``/etc/matrx/bridge-env.sh``
(``sandbox-image/scripts/write-bridge-env.sh``, which carries the ruling on why
a shell may hold the service token), and this module reads that file when — and
only when — a required variable is absent from the process environment. The
process environment always wins.

Every outbound AI Dream call in this image builds its headers here: the
cloud-files bridge clients (``matrx_agent.cloud_sync.client``), the ``mtx
files`` CLI, and the Browser Manager client (``matrx_tools.browser_manager``).
Shell callers get the same contract from
``sandbox-image/scripts/bridge-headers.sh``.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
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


#: The identity every entrypoint publishes out of the container environment for
#: callers sshd handed nothing (see the module docstring).
PUBLISHED_IDENTITY_FILE = Path(
    os.environ.get("MATRX_BRIDGE_ENV_FILE", "/etc/matrx/bridge-env.sh")
)


def load_published_identity(
    path: Optional[Path] = None, env: Optional[dict] = None
) -> list[str]:
    """Fill absent bridge variables from the published identity file.

    Returns the names it filled in. A value already present in the environment
    is never overridden, an unreadable or absent file is simply nothing to add
    (an unwired image is not a defect), and a line this parser does not
    understand is skipped rather than guessed at.
    """
    target = os.environ if env is None else env
    source = PUBLISHED_IDENTITY_FILE if path is None else path
    try:
        text = Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    filled: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) != 2 or "=" not in parts[1]:
            continue
        name, _, value = parts[1].partition("=")
        if name not in REQUIRED_BRIDGE_ENV or not value.strip():
            continue
        if (target.get(name) or "").strip():
            continue
        target[name] = value
        filled.append(name)
    return filled


def missing_bridge_env(env: Optional[Mapping[str, str]] = None) -> list[str]:
    """Names of the required bridge environment variables that are unset.

    When something is missing from the process environment, the published
    identity file is consulted first — that is what makes the ``mtx`` CLI work
    in an SSH session, where the container env never arrives.
    """
    if env is None:
        if [name for name in REQUIRED_BRIDGE_ENV if not (os.environ.get(name) or "").strip()]:
            load_published_identity()
        source: Mapping[str, str] = os.environ
    else:
        source = env
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
    "PUBLISHED_IDENTITY_FILE",
    "ORGANIZATION_ID_ENV",
    "ORGANIZATION_ID_HEADER",
    "REMEDY",
    "REQUIRED_BRIDGE_ENV",
    "USER_ID_ENV",
    "USER_ID_HEADER",
    "identity_headers",
    "load_published_identity",
    "missing_bridge_env",
]
