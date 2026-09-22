"""Secret VALUES never reach a persisted ``sandbox_instances`` row.

Incident (2026-09-22): three live rows carried the owner's DECRYPTED vault
secrets in plain text under ``config.env`` — among them a GitHub token and a
Bright Data API key — because ``POST /sandboxes`` accepts a caller
``config.env`` for the ``docker run -e KEY=value`` merge and the whole
``config`` dict was then serialized into the row. A row is readable by the
admin portals, MCP ``execute_sql``, exports, log pipelines and agent
transcripts; a container's process environment is not.

These are failing-then-passing guards: revert
``orchestrator/secret_redaction.py``'s use in ``store._config_with_boot``
and ``test_persisted_config_never_carries_env_values`` fails on the real
serializer, not on a mock of it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.secret_redaction import (
    config_carries_secret_values,
    redact_secret_values,
)
from orchestrator.store import _config_with_boot

SECRET_VALUE = "ghp_ThisIsNotARealTokenItIsATestFixture0001"


def _sandbox(config: dict) -> SandboxResponse:
    return SandboxResponse(
        sandbox_id="sbx-testfixture01",
        user_id="4cf62e4e-2679-484f-b652-034e697418df",
        organization_id="11111111-1111-1111-1111-111111111111",
        name="guard",
        status=SandboxStatus.CREATING,
        created_at=datetime.now(timezone.utc),
        config=config,
        ttl_seconds=7200,
    )


def test_persisted_config_never_carries_env_values() -> None:
    """The ONE serializer both INSERT paths use drops the values."""
    sandbox = _sandbox(
        {
            "organization_id": "11111111-1111-1111-1111-111111111111",
            "env": {
                "GITHUB_TOKEN": SECRET_VALUE,
                "BRIGHTDATA_API_KEY": "brd_test_fixture_value_0002",
            },
        }
    )

    persisted = json.loads(_config_with_boot(sandbox))

    assert "env" not in persisted, (
        "config.env survived serialization — secret values are at rest again"
    )
    blob = json.dumps(persisted)
    assert SECRET_VALUE not in blob
    assert "brd_test_fixture_value_0002" not in blob

    # What a row MAY carry: the names, the count, and when it happened.
    names = persisted["env_names"]
    assert names["names"] == ["BRIGHTDATA_API_KEY", "GITHUB_TOKEN"]
    assert names["count"] == 2
    assert names["redacted_at"]
    # Unrelated keys are untouched — this runs on every write.
    assert persisted["organization_id"] == "11111111-1111-1111-1111-111111111111"


def test_config_without_secrets_round_trips_unchanged() -> None:
    """Redaction is safe to apply to every write, not only leaky ones."""
    original = {
        "organization_id": "11111111-1111-1111-1111-111111111111",
        "secrets_injection": {"names": ["GITHUB_TOKEN"], "fetched_count": 1},
        "platform_env": {"forwarded": ["MATRX_AIDREAM_URL"]},
        "workspace_key": "primary",
    }
    assert redact_secret_values(original) == original


def test_empty_env_leaves_no_ornament() -> None:
    assert redact_secret_values({"env": {}}) == {}
    assert redact_secret_values({"env": None}) == {}


def test_non_mapping_env_is_dropped_not_stored() -> None:
    """A bogus shape is refused, never persisted verbatim."""
    assert "env" not in redact_secret_values({"env": "GITHUB_TOKEN=" + SECRET_VALUE})


@pytest.mark.parametrize(
    "config,expected",
    [
        ({}, False),
        ({"env": {}}, False),
        ({"env": None}, False),
        ({"secrets_injection": {"names": ["A"]}}, False),
        ({"env": {"GITHUB_TOKEN": SECRET_VALUE}}, True),
        ({"env": "A=B"}, True),
    ],
)
def test_leak_predicate(config: dict, expected: bool) -> None:
    """One definition of 'carries secret values', shared with the live guard."""
    assert config_carries_secret_values(config) is expected


def test_sandbox_response_from_create_path_is_redacted() -> None:
    """The reservation helper hands the store an already-clean config."""
    from orchestrator import sandbox_manager

    built = sandbox_manager.redact_secret_values(
        {"env": {"GITHUB_TOKEN": SECRET_VALUE}, "organization_id": "org"}
    )
    assert not config_carries_secret_values(built)
