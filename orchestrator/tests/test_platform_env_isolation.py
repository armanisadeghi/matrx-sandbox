"""Platform credentials never reach a user's sandbox.

Incident 2026-09-13 (docs/incidents/2026-09-13-platform-env-leak.md): the
"aidream-in-sandbox env passthrough" forwarded every name in aidream's .env
plus ``settings.aidream_passthrough_env`` into EVERY container, whatever the
template. A ``slim`` box ran ``env`` and read the platform's database URL,
admin tokens, the bridge service token and provider API keys.

The class, and the guards:

1. Passthrough applies ONLY to the internal ``aidream`` template. Every other
   template's env is the orchestrator-managed identity/storage vars, the
   caller's ``config.env`` and the user's vault secrets — nothing from the
   orchestrator's own process environment.
2. Even the ``aidream`` template never receives names that look like master
   credentials unless the ``aidream_template_forwards_master_credentials``
   knob (feature ``infrastructure.sandbox``, default OFF) is on.
3. Migration refreshes obey the same two rules, and strip previously leaked
   platform names from a non-aidream box's env.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.store import InMemorySandboxStore
from tests.conftest import seed_sandbox_knobs, seed_store_sandbox_knobs

ORG_ID = "22222222-2222-4222-8222-222222222222"

# A representative slice of what the passthrough registry names in
# production (explicit list in config.py + the keys of aidream's .env).
PLATFORM_ENV = {
    "MATRX_DATABASE_URL": "postgresql://matrx:master@pooler/db",
    "SUPABASE_MATRIX_PASSWORD": "db-master-pw",
    "SUPABASE_MATRIX_JWT_SECRET": "jwt-master",
    "POSTGRES_PASSWORD": "pg-master-pw",
    "ADMIN_AUTH_TOKEN": "admin-master-token",
    "ADMIN_API_TOKEN": "admin-api-token",
    "AIDREAM_SANDBOX_SERVICE_TOKEN": "bridge-master-token",
    "GITHUB_CLIENT_SECRET": "gh-secret",
    "SHOPIFY_API_ACCESS_TOKEN": "shopify-token",
    "CEREBRAS_API_KEY_PERSONAL": "cerebras-key",
    "OPENAI_API_KEY": "openai-key",
    "MATRX_DM_DIRECT_CONNECTION_STRING": "postgresql://x:y@z/w",
    # Non-credential platform config that the aidream template legitimately
    # needs and that no deny pattern should catch.
    "SUPABASE_MATRIX_HOST": "db.matrxserver.com",
    "SUPABASE_MATRIX_PORT": "5432",
    "MATRX_ENV": "production",
    "LOG_LEVEL": "INFO",
}


@pytest.fixture(autouse=True)
def _platform_environ(monkeypatch):
    """Put a fake platform environment in the orchestrator's process env and
    make the passthrough registry name every key of it."""
    for key, value in PLATFORM_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "orchestrator.sandbox_manager._resolve_passthrough_keys",
        lambda: sorted(PLATFORM_ENV),
    )


@pytest.fixture
def created_env(monkeypatch, tmp_path):
    """Create a sandbox with a fully mocked docker client; return a callable
    that yields the ``environment=`` dict handed to ``containers.run``."""
    from docker.errors import NotFound

    from orchestrator import sandbox_manager
    from orchestrator.config import settings
    from orchestrator.hosted_migration import HostedMigrationJournal

    store = InMemorySandboxStore()
    seed_store_sandbox_knobs(store)
    journal_root = tmp_path / "migration-state"
    journal_root.mkdir(mode=0o700)
    journal = HostedMigrationJournal(journal_root)
    monkeypatch.setattr("orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr(settings, "host_tier", "ec2")
    monkeypatch.setattr(settings, "internal_development_workspace_root", str(tmp_path / "dev"))
    # No vault fetch in this test: the orchestrator's OWN bridge token must
    # not be what proves isolation.
    monkeypatch.setattr(settings, "aidream_url", "")
    monkeypatch.setattr(settings, "aidream_service_token", "")
    monkeypatch.setattr("os.chown", MagicMock())
    monkeypatch.setattr(sandbox_manager, "_store", store)
    monkeypatch.setattr(sandbox_manager, "_docker_client", None)

    client = MagicMock()
    client.volumes.get.side_effect = NotFound("absent")
    container = MagicMock()
    container.id = "cid"
    container.status = "running"
    container.exec_run.return_value = (0, b"")
    client.containers.run.return_value = container
    client.containers.get.return_value = container
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)

    async def _create(template: str, **config_env: str) -> dict[str, str]:
        config = {"env": dict(config_env)} if config_env else {}
        if template == "development":
            config["workspace_key"] = "primary"
        await sandbox_manager.create_sandbox(
            user_id="00000000-0000-4000-8000-000000000001",
            organization_id=ORG_ID,
            template=template,
            tier="ec2",
            config=config,
        )
        return dict(client.containers.run.call_args.kwargs["environment"])

    return _create


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["slim", "development"])
async def test_non_aidream_templates_receive_no_platform_env(created_env, template):
    env = await created_env(template, GITHUB_TOKEN="user-vault-or-config-token")
    leaked = sorted(set(env) & set(PLATFORM_ENV))
    assert leaked == [], f"{template} box received platform env: {leaked}"
    # What the box IS allowed to carry: identity/storage + the caller's env.
    assert env["SANDBOX_TEMPLATE"] == template
    assert env["USER_ID"] == "00000000-0000-4000-8000-000000000001"
    assert env["GITHUB_TOKEN"] == "user-vault-or-config-token"


@pytest.mark.asyncio
async def test_aidream_template_keeps_config_but_never_master_credentials(created_env):
    env = await created_env("aidream")
    # Non-credential platform config flows (this is the whole point of the
    # aidream template).
    assert env["SUPABASE_MATRIX_HOST"] == "db.matrxserver.com"
    assert env["MATRX_ENV"] == "production"
    assert env["LOG_LEVEL"] == "INFO"
    # Master credentials are denied by default.
    denied = {
        "MATRX_DATABASE_URL", "SUPABASE_MATRIX_PASSWORD", "SUPABASE_MATRIX_JWT_SECRET",
        "POSTGRES_PASSWORD", "ADMIN_AUTH_TOKEN", "ADMIN_API_TOKEN",
        "AIDREAM_SANDBOX_SERVICE_TOKEN", "GITHUB_CLIENT_SECRET",
        "SHOPIFY_API_ACCESS_TOKEN", "CEREBRAS_API_KEY_PERSONAL", "OPENAI_API_KEY",
        "MATRX_DM_DIRECT_CONNECTION_STRING",
    }
    present = sorted(denied & set(env))
    assert present == [], f"aidream box received master credentials with the knob OFF: {present}"


@pytest.mark.asyncio
async def test_aidream_template_forwards_master_credentials_only_when_knob_is_on(created_env):
    seed_sandbox_knobs({"aidream_template_forwards_master_credentials": True})
    env = await created_env("aidream")
    assert env["SUPABASE_MATRIX_PASSWORD"] == "db-master-pw"
    assert env["OPENAI_API_KEY"] == "openai-key"


@pytest.mark.asyncio
async def test_knob_on_never_opens_non_aidream_templates(created_env):
    seed_sandbox_knobs({"aidream_template_forwards_master_credentials": True})
    env = await created_env("slim")
    assert sorted(set(env) & set(PLATFORM_ENV)) == []


@pytest.mark.asyncio
async def test_missing_knob_row_fails_closed(created_env, caplog):
    """A missing ``platform.feature_knob`` row must never open the door."""
    from orchestrator import knobs

    knobs.clear_knob_cache()
    seed_sandbox_knobs()  # the 0636 values only; no master-credentials row
    with caplog.at_level("WARNING"):
        env = await created_env("aidream")
    assert "SUPABASE_MATRIX_PASSWORD" not in env
    assert "aidream_template_forwards_master_credentials" in caplog.text


def test_vault_secrets_are_never_filtered():
    """The deny-list is about the orchestrator's OWN environment. A user's
    vault secret named like a credential is the user's to hand to their box."""
    from orchestrator.sandbox_manager import is_master_credential_name

    assert is_master_credential_name("OPENAI_API_KEY")
    # ...and yet the vault merge (create_sandbox step 3) applies no filter —
    # asserted end-to-end below via config.env, which rides the same
    # unfiltered merge.


@pytest.mark.asyncio
async def test_callers_own_env_rides_unfiltered(created_env):
    env = await created_env("slim", OPENAI_API_KEY="users-own-key")
    assert env["OPENAI_API_KEY"] == "users-own-key"


@pytest.mark.parametrize(
    "name,denied",
    [
        ("SUPABASE_MATRIX_PASSWORD", True),
        ("DATA_FOR_SEO_PASSWORD", True),
        ("GITHUB_CLIENT_SECRET", True),
        ("SUPABASE_MATRIX_JWT_SECRET", True),
        ("SHOPIFY_API_SECRET_KEY", True),
        ("MATRX_DATABASE_URL", True),
        ("SUPABASE_DATABASE_URL_POOLER", True),
        ("AIDREAM_SANDBOX_SERVICE_TOKEN", True),
        ("ADMIN_AUTH_TOKEN", True),
        ("ADMIN_API_TOKEN", True),
        ("OPENAI_API_KEY", True),
        ("CEREBRAS_API_KEY_PERSONAL", True),
        ("SHOPIFY_API_ACCESS_TOKEN", True),
        ("REPLICATE_API_TOKEN", True),
        ("MATRX_DM_DIRECT_CONNECTION_STRING", True),
        ("AWS_SECRET_ACCESS_KEY", True),
        ("GOOGLE_APPLICATION_CREDENTIALS", True),
        ("SUPABASE_MATRIX_HOST", False),
        ("SUPABASE_MATRIX_PORT", False),
        ("MATRX_ENV", False),
        ("LOG_LEVEL", False),
        ("ADMIN_USER_ID", False),
        ("TOOL_WORKSPACE_BASE", False),
    ],
)
def test_master_credential_patterns(name, denied):
    from orchestrator.sandbox_manager import is_master_credential_name

    assert is_master_credential_name(name) is denied


# ── Migration refresh obeys the same rules ──────────────────────────────────

def test_migration_refresh_strips_leaked_platform_env_from_slim_box():
    from orchestrator.migrate import _refresh_platform_environment

    refreshed, changed = _refresh_platform_environment(
        ["SUPABASE_MATRIX_PASSWORD=db-master-pw", "USER_CHOSEN_VALUE=keep-me",
         "MATRX_DATABASE_URL=postgresql://leaked"],
        template="slim",
        allow_master_credentials=False,
    )
    assert refreshed == ["USER_CHOSEN_VALUE=keep-me"]
    assert changed == 2


def test_migration_refresh_denies_master_credentials_for_aidream_by_default():
    from orchestrator.migrate import _refresh_platform_environment

    refreshed, _ = _refresh_platform_environment(
        ["SUPABASE_MATRIX_PASSWORD=old", "SUPABASE_MATRIX_HOST=old-host"],
        template="aidream",
        allow_master_credentials=False,
    )
    assert "SUPABASE_MATRIX_HOST=db.matrxserver.com" in refreshed
    assert not any(item.startswith("SUPABASE_MATRIX_PASSWORD=") for item in refreshed)
