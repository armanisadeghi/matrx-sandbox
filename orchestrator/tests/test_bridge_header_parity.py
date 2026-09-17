"""The orchestrator's header builder is a MIRROR — and mirrors drift.

The orchestrator and the sandbox image ship as separate Python distributions
(the host never installs the in-container ``matrx_agent`` SDK, and the SDK must
install in a box that has no orchestrator), so the one builder exists twice.
Twice is a liability: this test is what makes it a mirror rather than a fork.

Until 2026-09-17 the orchestrator had no builder at all —
``sandbox_manager.create_sandbox`` hand-wrote ``X-Matrx-User-Id`` and
``X-Organization-Id`` inline, outside every guard the image had.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orchestrator.bridge_headers import (
    BridgeIdentityMissing,
    ORGANIZATION_ID_HEADER,
    REQUIRED_BRIDGE_ENV,
    USER_ID_HEADER,
    identity_headers,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_BUILDER = REPO_ROOT / "sandbox-image" / "sdk" / "matrx_agent" / "bridge_headers.py"


def _sdk_constant(name: str) -> str:
    text = SDK_BUILDER.read_text(encoding="utf-8")
    match = re.search(rf'^{name} = "(.+)"$', text, re.MULTILINE)
    assert match, f"{name} not found in {SDK_BUILDER}"
    return match.group(1)


def test_header_names_match_the_image_builder() -> None:
    assert USER_ID_HEADER == _sdk_constant("USER_ID_HEADER")
    assert ORGANIZATION_ID_HEADER == _sdk_constant("ORGANIZATION_ID_HEADER")


def test_required_variable_list_matches_the_image_builder() -> None:
    text = SDK_BUILDER.read_text(encoding="utf-8")
    block = text.split("REQUIRED_BRIDGE_ENV = (", 1)[1].split(")", 1)[0]
    sdk_names = re.findall(r'"([A-Z_]+)"', block) + re.findall(r"\b([A-Z_]+_ENV)\b", block)
    # The SDK spells two entries as constants (USER_ID_ENV / ORGANIZATION_ID_ENV).
    resolved = ["USER_ID" if n == "USER_ID_ENV" else n for n in sdk_names]
    resolved = ["ORGANIZATION_ID" if n == "ORGANIZATION_ID_ENV" else n for n in resolved]
    assert list(REQUIRED_BRIDGE_ENV) == resolved


def test_the_builder_refuses_to_send_half_the_context() -> None:
    with pytest.raises(BridgeIdentityMissing, match="ORGANIZATION_ID"):
        identity_headers(token="t", user_id="u", organization_id="")
    with pytest.raises(BridgeIdentityMissing, match="USER_ID"):
        identity_headers(token="t", user_id="", organization_id="org")
    with pytest.raises(BridgeIdentityMissing, match="MATRX_AIDREAM_SERVICE_TOKEN"):
        identity_headers(token="", user_id="u", organization_id="org")


def test_the_builder_sends_both_halves() -> None:
    headers = identity_headers(
        token="t", user_id="u", organization_id="org",
        extra={"User-Agent": "matrx-sandbox-orchestrator"},
    )

    assert headers["Authorization"] == "Bearer t"
    assert headers[USER_ID_HEADER] == "u"
    assert headers[ORGANIZATION_ID_HEADER] == "org"
    assert headers["User-Agent"] == "matrx-sandbox-orchestrator"


def test_the_user_secrets_fetch_goes_through_the_builder() -> None:
    """The one orchestrator → AI Dream call site, checked at its source."""
    source = (REPO_ROOT / "orchestrator" / "orchestrator" / "sandbox_manager.py").read_text(
        encoding="utf-8"
    )

    assert "identity_headers(" in source
    assert '"X-Matrx-User-Id"' not in source
    assert '"X-Organization-Id"' not in source
