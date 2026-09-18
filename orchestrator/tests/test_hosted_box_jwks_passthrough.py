"""A credential-free box still has to know WHO is calling it.

Chair ruling R14 (2026-09-18) makes the denial of master credentials to the
``aidream`` template permanent, so the in-box aidream cannot hold the shared
JWT secret. It does not need one: Matrx Main signs ES256, and verifying an
asymmetric signature needs only the project's PUBLIC JWKS document — the same
document every browser bundle fetches.

``MATRX_PLATFORM_AUTH_JWKS_URL`` carries that URL. These tests pin the three
properties that make it safe to forward, so a later edit to the denylist
patterns or the passthrough list cannot quietly turn it into a leak or quietly
stop delivering it (a box with no JWKS URL refuses every authenticated route).
"""

from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.sandbox_manager import (
    PLATFORM_PASSTHROUGH_TEMPLATES,
    is_master_credential_name,
    platform_passthrough_env,
)

JWKS_ENV = "MATRX_PLATFORM_AUTH_JWKS_URL"
JWKS_VALUE = "https://db.matrxserver.com/auth/v1/.well-known/jwks.json"


def test_the_jwks_url_is_not_a_master_credential() -> None:
    # A public document URL. If a future pattern catches it, a box silently
    # loses the ability to identify anyone and every run refuses.
    assert is_master_credential_name(JWKS_ENV) is False


def test_the_name_is_not_supabase_prefixed() -> None:
    # The blanket denial of SUPABASE_* to this template is the ruling and stays
    # exactly as it is; the box's public material must live outside that prefix.
    assert not JWKS_ENV.startswith("SUPABASE_")


def test_it_is_in_the_default_passthrough_list() -> None:
    assert JWKS_ENV in Settings().aidream_passthrough_env.split(",")


def test_it_reaches_the_aidream_template_while_the_password_stays_denied() -> None:
    env, denied = platform_passthrough_env(
        "aidream",
        allow_master_credentials=False,
        environ={JWKS_ENV: JWKS_VALUE, "SUPABASE_MATRIX_PASSWORD": "not-in-a-box"},
    )
    assert env == {JWKS_ENV: JWKS_VALUE}
    assert denied == ["SUPABASE_MATRIX_PASSWORD"]


def test_no_other_template_receives_it() -> None:
    # Only the aidream template runs an in-box aidream, so only it has a reason
    # to hold this — and platform env reaching any other box is the 2026-09-13
    # incident class.
    assert PLATFORM_PASSTHROUGH_TEMPLATES == frozenset({"aidream"})
    for template in ("slim", "development", "core"):
        env, denied = platform_passthrough_env(
            template, allow_master_credentials=False, environ={JWKS_ENV: JWKS_VALUE}
        )
        assert env == {}
        assert denied == []
