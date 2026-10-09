"""A hosted host missing a box-identity address must say so at boot.

2026-10-09: ``MATRX_HOSTED_SESSION_AUTHORITY_URL`` was added to the box's
admission (aidream f0c9d2820c) and to the forward set (b77aa7f) but never set
on the hosted host. Every box born after that refused every signed-in call with
503 ``session_authority_unavailable``; the person saw "Could not verify your
session" on Claude Connect and on hosted runs, and the orchestrator said nothing.
"""

from __future__ import annotations

from orchestrator.main import HOSTED_BOX_IDENTITY_ENV, hosted_box_identity_warnings

FULL = {
    "MATRX_PLATFORM_AUTH_JWKS_URL": "https://db.matrxserver.com/auth/v1/.well-known/jwks.json",
    "MATRX_HOSTED_SESSION_AUTHORITY_URL": "https://server.app.matrxserver.com/auth/session/status",
}


def test_a_fully_wired_hosted_host_is_quiet() -> None:
    assert hosted_box_identity_warnings(FULL, "hosted") == []


def test_the_missing_session_authority_is_named() -> None:
    env = {k: v for k, v in FULL.items() if k != "MATRX_HOSTED_SESSION_AUTHORITY_URL"}
    warnings = hosted_box_identity_warnings(env, "hosted")
    assert len(warnings) == 1
    assert warnings[0].startswith("MATRX_HOSTED_SESSION_AUTHORITY_URL is unset")


def test_a_blank_value_counts_as_missing() -> None:
    env = dict(FULL, MATRX_PLATFORM_AUTH_JWKS_URL="  ")
    assert [w.split(" ")[0] for w in hosted_box_identity_warnings(env, "hosted")] == [
        "MATRX_PLATFORM_AUTH_JWKS_URL"
    ]


def test_every_box_identity_name_is_forwarded_to_boxes() -> None:
    from orchestrator.sandbox_manager import PLATFORM_ENV_ALLOWLIST

    for name, _ in HOSTED_BOX_IDENTITY_ENV:
        assert name in PLATFORM_ENV_ALLOWLIST


def test_the_ec2_tier_does_not_serve_hosted_boxes() -> None:
    assert hosted_box_identity_warnings({}, "ec2") == []
