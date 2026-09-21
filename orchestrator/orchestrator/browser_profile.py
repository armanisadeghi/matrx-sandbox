"""Which cloud browser this box's owner has — asked of AI Dream, never of the DB.

THE GAP THIS CLOSES (2026-09-20). The in-box browser client
(``sandbox-image/sdk/matrx_tools/browser_manager.py``, shipped 2026-08-20) fails
closed without ``MATRX_BROWSER_PROFILE_ID`` and
``MATRX_BROWSER_EXECUTION_TARGET``, and the orchestrator never set them. A
sandbox and the person's persistent cloud browser have therefore been two
separate things for a month, with one missing lookup between them.

WHY AN HTTP DOOR. The orchestrator holds exactly one platform DB connection and
uses it for ``sandbox_instances`` and the knob rows — nothing else. Reading
``browser.profile`` from here would make this host a second authority on who
owns which browser, with no RLS, no access check and no tenant proof. It asks
AI Dream instead, over the SAME server-to-server bridge the vault env fetch
already rides (``/api/sandboxes/internal/default-browser-profile``), and the
answer it gets back has already been membership-proved on the other side.

FAIL-OPEN, LOUDLY. A create must never die because the browser lookup did not
answer. When this returns ``None`` the box simply boots with no browser names,
the in-box client refuses with a sentence a person can act on, and the reason is
stamped on the row so a screen can say WHY rather than showing a dead control.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from orchestrator.bridge_headers import BridgeIdentityMissing, identity_headers
from orchestrator.config import settings

logger = logging.getLogger(__name__)

#: The env pair the in-box client reads. Named here so the create path, the
#: binding refresh and the guard set all spell them the same way once.
PROFILE_ID_ENV = "MATRX_BROWSER_PROFILE_ID"
EXECUTION_TARGET_ENV = "MATRX_BROWSER_EXECUTION_TARGET"
BROWSER_ENV_NAMES: tuple[str, ...] = (PROFILE_ID_ENV, EXECUTION_TARGET_ENV)

FETCH_TIMEOUT_SECONDS = 10.0

ROUTE = "/api/sandboxes/internal/default-browser-profile"


@dataclass(frozen=True)
class BrowserProfileLookup:
    """What the platform said. Exactly one of ``profile_id`` / ``reason`` is real."""

    profile_id: str | None = None
    label: str | None = None
    execution_target: str | None = None
    reason: str | None = None

    @property
    def present(self) -> bool:
        return bool(self.profile_id and self.execution_target)

    def env(self) -> dict[str, str]:
        """The two names to inject, or an empty dict when there is no browser."""
        if not self.present:
            return {}
        return {
            PROFILE_ID_ENV: str(self.profile_id),
            EXECUTION_TARGET_ENV: str(self.execution_target),
        }

    def diagnostic(self) -> dict[str, Any]:
        """Names and ids only — this is persisted on the sandbox row."""
        return {
            "present": self.present,
            "profile_id": self.profile_id,
            "label": self.label,
            "execution_target": self.execution_target,
            "reason": self.reason,
        }


async def resolve_browser_profile(
    *, user_id: str | None, organization_id: str | None
) -> BrowserProfileLookup:
    """Ask AI Dream for this person's default browser in this organization.

    Never raises. Every failure comes back as a ``reason`` a person can read.
    """
    url = settings.resolve_aidream_url()
    token = settings.resolve_aidream_service_token()
    if not user_id:
        return BrowserProfileLookup(reason="the create request named no user")
    if not organization_id:
        return BrowserProfileLookup(reason="the create request named no organization")
    if not url:
        return BrowserProfileLookup(
            reason=(
                "this orchestrator cannot reach AI Dream (MATRX_AIDREAM_URL is "
                "unset), so it could not look up your cloud browser"
            )
        )
    if not token:
        return BrowserProfileLookup(
            reason=(
                "this orchestrator has no AI Dream service token "
                "(MATRX_AIDREAM_SERVICE_TOKEN), so it could not look up your "
                "cloud browser"
            )
        )
    try:
        headers = identity_headers(
            token=token,
            user_id=str(user_id),
            organization_id=str(organization_id),
            extra={"User-Agent": "matrx-sandbox-orchestrator"},
        )
    except BridgeIdentityMissing as exc:
        return BrowserProfileLookup(reason=str(exc))

    import httpx

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as hx:
            resp = await hx.get(f"{url.rstrip('/')}{ROUTE}", headers=headers)
    except Exception as exc:  # noqa: BLE001 — network boundary, never fatal
        logger.warning(
            "browser profile lookup failed for user=%s org=%s: %s",
            user_id, organization_id, exc,
        )
        return BrowserProfileLookup(
            reason=f"the cloud browser lookup did not answer ({type(exc).__name__})"
        )
    if resp.status_code == 404:
        # An AI Dream that predates this door. Say so precisely — "no browser"
        # and "this server cannot answer yet" are different facts.
        return BrowserProfileLookup(
            reason=(
                "this AI Dream server does not yet serve the sandbox browser "
                "lookup; redeploy it and recreate or rebind this sandbox"
            )
        )
    if resp.status_code != 200:
        return BrowserProfileLookup(
            reason=(
                f"the cloud browser lookup answered HTTP {resp.status_code}: "
                f"{(resp.text or '')[:200]}"
            )
        )
    try:
        body = resp.json() or {}
    except Exception:  # noqa: BLE001
        return BrowserProfileLookup(reason="the cloud browser lookup returned no JSON")
    if not isinstance(body, dict):
        return BrowserProfileLookup(reason="the cloud browser lookup returned no JSON object")
    if not body.get("present"):
        return BrowserProfileLookup(
            reason=str(body.get("reason") or "you have no cloud browser here yet")
        )
    profile_id = str(body.get("profile_id") or "")
    target = str(body.get("execution_target") or "")
    if not profile_id or not target:
        return BrowserProfileLookup(
            reason="the cloud browser lookup answered without an id and a target"
        )
    return BrowserProfileLookup(
        profile_id=profile_id,
        label=str(body.get("label") or "") or None,
        execution_target=target,
    )
