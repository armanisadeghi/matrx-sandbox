"""The orchestrator's header builder is a MIRROR — and mirrors drift.

The orchestrator and the sandbox image ship as separate Python distributions
(the host never installs the in-container ``matrx_agent`` SDK, and the SDK must
install in a box that has no orchestrator), so the one builder exists twice.
Twice is a liability: this test is what makes it a mirror rather than a fork.

Until 2026-09-17 the orchestrator had no builder at all —
``sandbox_manager.create_sandbox`` hand-wrote ``X-Matrx-User-Id`` and
``X-Organization-Id`` inline, outside every guard the image had.

HOW THIS TEST WORKS, AND WHY IT CHANGED
---------------------------------------
It used to compare the two files' CONSTANTS with regexes, and its list check
depended on the SDK spelling two entries as quoted names before the rest as
``*_ENV`` constants — so a harmless reformat failed it, while a real
divergence in what the builders DO (a different refusal, a header added on one
side only, a value trimmed on one side only) sailed through: a regex cannot see
behaviour. It now IMPORTS the image's builder and calls both with the same
inputs, asserting the same header dict and the same refusal. That is a forcing
function: it can only pass when the two really behave alike.
"""

from __future__ import annotations

import importlib.util
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


def _load_image_builder():
    """Import the image's builder from source.

    Loaded by path, not by package import: the orchestrator's environment does
    not (and must not) have ``matrx_agent`` installed, and the builder is
    deliberately stdlib-only so this works anywhere the repo is checked out.
    """
    spec = importlib.util.spec_from_file_location(
        "matrx_agent_bridge_headers_under_test", SDK_BUILDER
    )
    assert spec and spec.loader, SDK_BUILDER
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


image_builder = _load_image_builder()


def test_header_names_match_the_image_builder() -> None:
    assert USER_ID_HEADER == image_builder.USER_ID_HEADER
    assert ORGANIZATION_ID_HEADER == image_builder.ORGANIZATION_ID_HEADER


def test_required_variable_list_matches_the_image_builder() -> None:
    """Values, in order — never how either file happens to spell them."""
    assert list(REQUIRED_BRIDGE_ENV) == list(image_builder.REQUIRED_BRIDGE_ENV)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"token": "t", "user_id": "u", "organization_id": "org"},
        {"token": "t", "user_id": "u", "organization_id": "org", "accept": None},
        {
            "token": "t",
            "user_id": "u",
            "organization_id": "org",
            "extra": {"User-Agent": "matrx-sandbox-orchestrator"},
        },
    ],
    ids=["default", "no-accept", "with-extra"],
)
def test_both_builders_produce_the_same_headers_for_the_same_call(kwargs) -> None:
    assert identity_headers(**kwargs) == image_builder.identity_headers(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "named"),
    [
        ({"token": "t", "user_id": "u", "organization_id": ""}, "ORGANIZATION_ID"),
        ({"token": "t", "user_id": "", "organization_id": "org"}, "USER_ID"),
        ({"token": "", "user_id": "u", "organization_id": "org"}, "MATRX_AIDREAM_SERVICE_TOKEN"),
        ({"token": "  ", "user_id": " ", "organization_id": " "}, "USER_ID"),
    ],
    ids=["no-org", "no-user", "no-token", "all-blank"],
)
def test_both_builders_refuse_the_same_call_and_name_the_same_variable(
    kwargs, named
) -> None:
    """A refusal is part of the contract: one side quietly accepting what the
    other refuses is exactly the drift this file exists to catch."""
    with pytest.raises(BridgeIdentityMissing) as host:
        identity_headers(**kwargs)
    with pytest.raises(image_builder.BridgeIdentityMissing) as image:
        image_builder.identity_headers(**kwargs)

    # Both name every variable behind the refusal. The two REMEDY sentences
    # differ on purpose — one tells an operator to fix the orchestrator's call,
    # the other tells a person to recreate their sandbox — but neither may be
    # vague, and neither may accept what the other refuses.
    assert named in str(host.value)
    assert named in str(image.value)
    assert "organization" in str(host.value).lower()
    assert "organization" in str(image.value).lower()


def test_the_user_secrets_fetch_goes_through_the_builder() -> None:
    """The one orchestrator → AI Dream call site, checked at its source."""
    source = (REPO_ROOT / "orchestrator" / "orchestrator" / "sandbox_manager.py").read_text(
        encoding="utf-8"
    )

    assert "identity_headers(" in source
    assert '"X-Matrx-User-Id"' not in source
    assert '"X-Organization-Id"' not in source


# ── The other direction: verifying what a sandbox forwards ──────────────────


def test_a_forwarded_identity_that_matches_the_row_verifies() -> None:
    from types import SimpleNamespace

    from orchestrator.bridge_headers import verify_forwarded_identity

    sandbox = SimpleNamespace(sandbox_id="sbx-1", user_id="u", organization_id="org")

    assert (
        verify_forwarded_identity(
            sandbox, {USER_ID_HEADER: "u", ORGANIZATION_ID_HEADER: "org"}
        )
        == "verified"
    )
    # An image that predates the contract sends neither header: accepted, and
    # reported as unverified rather than counted as a match.
    assert verify_forwarded_identity(sandbox, {}) == "unverified"


def test_the_headers_a_sandbox_forwards_are_the_ones_this_side_reads() -> None:
    """Behaviour, end to end: build on the image side, verify on the host side."""
    from types import SimpleNamespace

    from orchestrator.bridge_headers import verify_forwarded_identity

    headers = image_builder.actor_headers(user_id="u", organization_id="org")
    sandbox = SimpleNamespace(sandbox_id="sbx-1", user_id="u", organization_id="org")

    assert verify_forwarded_identity(sandbox, headers) == "verified"
