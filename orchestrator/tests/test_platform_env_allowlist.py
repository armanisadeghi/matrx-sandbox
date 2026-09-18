"""The platform env a box receives is an ALLOWLIST, and a denylist can never
come back.

XT-10, feedback 34dcf28a (register /Users/armanisadeghi/code/common-docs/
projects/coding-agent-bridge/REGISTER.md, ruling R14). Measured on the real
hosted box ``sbx-7bc1060b325f``: the orchestrator reported ``denied_count=72``
and the box looked protected, while 14 secret-shaped names — including a live
``ANTHROPIC_KEY`` (``sk-ant-api03-…``), ``MATRX_AGENT_TOKEN``,
``MATRX_SCRAPER_TOKEN`` and five ``SUPABASE_*_KEY`` names — sat in the
environment of a container the person has a shell in. None of them matched a
pattern in ``MASTER_CREDENTIAL_PATTERNS``: ``.*_API_KEY.*`` does not match
``ANTHROPIC_KEY``, ``.*_SERVICE_TOKEN.*`` does not match ``MATRX_AGENT_TOKEN``,
and a Supabase service-role key is spelled ``..._KEY``, not ``..._SECRET``.

Every test here is RED on the pre-fix code (the denylist forwards each of these
names) and GREEN on the allowlist. The two in this file that matter most:

* ``test_the_fourteen_names_never_reach_a_container`` — the exact names from the
  live measurement, planted in the host env, asserted absent from the rendered
  container env.
* ``test_allowlist_rejects_an_unknown_name`` — a name nobody has thought about
  is withheld BY DEFAULT, which is the whole point of inverting the failure
  mode. A denylist passes this only by accident.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.store import InMemorySandboxStore
from tests.conftest import seed_sandbox_knobs, seed_store_sandbox_knobs

READY_PROBE = (0, b"phase=ready\nprogress=\nready=yes\nsdk=yes\n")

ORG_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "00000000-0000-4000-8000-000000000001"

#: The 14 names measured in sbx-7bc1060b325f's own ``agent-env`` on 2026-09-18,
#: with stand-in values. NOT ONE of them matches a master-credential pattern,
#: which is exactly why the denylist forwarded them.
THE_FOURTEEN = {
    "ANTHROPIC_KEY": "sk-ant-api03-planted-not-real",
    "MATRX_AGENT_TOKEN": "planted-agent-token",
    "MATRX_SCRAPER_TOKEN": "planted-scraper-token",
    "SUPABASE_KEY": "planted-service-role-key",
    "SUPABASE_MATRIX_KEY": "planted-service-role-key",
    "SUPABASE_DJANGO_KEY": "planted-service-role-key",
    "SUPABASE_MATRIX_DJANGO_KEY": "planted-service-role-key",
    "SUPABASE_AI_MATRIX_KEY": "planted-service-role-key",
    "SUPABASE_SAMPLE_MATRIX_KEY": "planted-service-role-key",
    "SUPABASE_MATRIX_PUBLISHABLE_KEY": "planted-publishable-key",
    "TENSORDOCK_AUTH_KEY": "planted-tensordock-key",
    "MATRX_ENGINE_TENSOR_DOCK_SERVER_KEY": "planted-tensordock-key",
    "HUGGING_FACE_TOKEN_ID": "planted-hf-token",
    "MATRX_REDACTION_KMS_KEY_ID": "planted-kms-key-id",
}

#: Secret shapes the brief names explicitly, plus the connection bits the box
#: was holding every part of except the password.
OTHER_PLANTED = {
    "SUPABASE_SERVICE_KEY": "planted-service-key",
    "SOME_VENDOR_TOKEN": "planted-vendor-token",
    "SUPABASE_MATRIX_HOST": "db.matrxserver.com",
    "SUPABASE_MATRIX_PORT": "5432",
    "SUPABASE_MATRIX_USER": "postgres.matrx",
    "SUPABASE_MATRIX_DATABASE_NAME": "postgres",
    # A name nobody has thought about — the next unnamed secret.
    "BRAND_NEW_PROVIDER_CREDENTIAL": "planted-future-secret",
}

#: Legitimately forwarded: the ONE public thing a credential-free box needs.
ALLOWED_PLANTED = {
    "MATRX_PLATFORM_AUTH_JWKS_URL": "https://db.matrxserver.com/auth/v1/.well-known/jwks.json",
    "MATRX_ENV": "production",
    "LOG_LEVEL": "INFO",
}

HOST_ENV = {**THE_FOURTEEN, **OTHER_PLANTED, **ALLOWED_PLANTED}


@pytest.fixture(autouse=True)
def _host_environ(monkeypatch):
    """Plant the secrets in the ORCHESTRATOR HOST's env and make the passthrough
    registry name every one of them — which is the real situation: the registry
    is aidream's own ``.env`` file, so every name in it is named automatically."""
    for key, value in HOST_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "orchestrator.sandbox_manager._resolve_passthrough_keys",
        lambda: sorted(HOST_ENV),
    )


@pytest.fixture
def created_env(monkeypatch, tmp_path):
    """Create a sandbox against a mocked docker client; return the
    ``environment=`` dict actually handed to ``containers.run``."""
    from docker.errors import NotFound

    from orchestrator import sandbox_manager
    from orchestrator.config import settings
    from orchestrator.hosted_migration import HostedMigrationJournal

    store = InMemorySandboxStore()
    seed_store_sandbox_knobs(store)
    journal_root = tmp_path / "migration-state"
    journal_root.mkdir(mode=0o700)
    journal = HostedMigrationJournal(journal_root)
    monkeypatch.setattr(
        "orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal
    )
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr(settings, "host_tier", "ec2")
    monkeypatch.setattr(settings, "internal_development_workspace_root", str(tmp_path / "dev"))
    # No vault fetch: the orchestrator's own bridge token must not be what
    # proves isolation.
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
    container.exec_run.return_value = READY_PROBE
    client.containers.run.return_value = container
    client.containers.get.return_value = container
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)

    async def _create(template: str) -> dict[str, str]:
        await sandbox_manager.create_sandbox(
            user_id=USER_ID,
            organization_id=ORG_ID,
            template=template,
            tier="ec2",
            config={},
        )
        return dict(client.containers.run.call_args.kwargs["environment"])

    return _create


# ── The measured leak, as a test ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_fourteen_names_never_reach_a_container(created_env):
    """RED on the denylist: all 14 are forwarded, because no pattern names
    them. GREEN on the allowlist: none of them is on it."""
    env = await created_env("aidream")
    present = sorted(set(THE_FOURTEEN) & set(env))
    assert present == [], (
        "aidream box received secret-shaped platform names the pattern denylist "
        f"missed: {present}"
    )
    # And no VALUE of any of them is anywhere in the rendered env, whatever the
    # name it might have been re-spelled under.
    values = set(THE_FOURTEEN.values()) | set(OTHER_PLANTED.values())
    planted_values_present = sorted(
        f"{k}={v}" for k, v in env.items() if v in values and k not in OTHER_PLANTED
    )
    assert planted_values_present == [], planted_values_present


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_KEY", "SUPABASE_SERVICE_KEY", "MATRX_AGENT_TOKEN", "SOME_VENDOR_TOKEN"],
)
async def test_planted_host_secrets_are_absent_from_the_rendered_env(created_env, name):
    """The brief's named plants: ANTHROPIC_KEY / SUPABASE_SERVICE_KEY / any
    ``*_TOKEN`` in the host env, asserted absent from the container env."""
    env = await created_env("aidream")
    assert name not in env or env[name] != HOST_ENV[name]


@pytest.mark.asyncio
async def test_allowlist_rejects_an_unknown_name(created_env):
    """A name nobody has thought about is withheld BY DEFAULT — the inverted
    failure mode. A denylist forwards it."""
    env = await created_env("aidream")
    assert "BRAND_NEW_PROVIDER_CREDENTIAL" not in env


def test_allowlist_rejects_an_unknown_name_at_the_decision(monkeypatch):
    """Same rule at the pure decision function, with no docker in the way."""
    from orchestrator.sandbox_manager import platform_env_decision

    decision = platform_env_decision(
        "aidream", allow_master_credentials=False, environ=dict(HOST_ENV)
    )
    assert "BRAND_NEW_PROVIDER_CREDENTIAL" not in decision.forwarded
    assert "BRAND_NEW_PROVIDER_CREDENTIAL" in decision.withheld_not_allowlisted
    assert sorted(decision.forwarded) == sorted(ALLOWED_PLANTED)


@pytest.mark.asyncio
async def test_the_one_public_name_a_credential_free_box_needs_still_arrives(created_env):
    """Fail-closed must not mean useless: the public JWKS document URL is how a
    box with no secret knows who is calling it (XT-08b)."""
    env = await created_env("aidream")
    assert env["MATRX_PLATFORM_AUTH_JWKS_URL"] == ALLOWED_PLANTED[
        "MATRX_PLATFORM_AUTH_JWKS_URL"
    ]


@pytest.mark.asyncio
async def test_orchestrator_set_path_overrides_survive_the_allowlist(created_env):
    """The aidream template's path-shape overrides are the ORCHESTRATOR's own
    values, not passthrough — they must still land."""
    env = await created_env("aidream")
    assert env["BASE_DIR"] == "/home/agent/aidream"
    assert env["TOOL_WORKSPACE_BASE"] == "/home/agent"


# ── Nothing fails silently ───────────────────────────────────────────────────


def test_every_withheld_name_is_counted_and_named():
    """``denied_count=72`` counted pattern hits only. Now the report accounts
    for EVERY host-set registry name: forwarded + withheld = the whole set."""
    from orchestrator.sandbox_manager import platform_env_decision

    decision = platform_env_decision(
        "aidream", allow_master_credentials=False, environ=dict(HOST_ENV)
    )
    accounted = (
        set(decision.forwarded)
        | set(decision.withheld_not_allowlisted)
        | set(decision.withheld_master_credential)
    )
    assert accounted == set(HOST_ENV)
    report = decision.report()
    assert report["withheld_not_allowlisted_count"] == len(
        decision.withheld_not_allowlisted
    )
    # The names, not just a count — an operator must be able to see WHAT the
    # box did not get.
    for name in THE_FOURTEEN:
        assert name in report["denied_names"]


@pytest.mark.asyncio
async def test_the_boot_report_names_the_withheld_secrets(created_env, caplog):
    with caplog.at_level("WARNING"):
        await created_env("aidream")
    assert "ANTHROPIC_KEY" in caplog.text
    assert "PLATFORM_ENV_ALLOWLIST" in caplog.text


# ── The patterns survive only as a guard over the allowlist ──────────────────


def test_a_secret_shaped_name_on_the_allowlist_refuses():
    """The secondary guard. The patterns no longer gate the registry; they
    police the allowlist, and a secret-shaped entry refuses LOUDLY instead of
    handing a real credential to every box of the template."""
    from orchestrator.sandbox_manager import _assert_allowlist_holds_no_secret_shapes

    with pytest.raises(RuntimeError) as exc:
        _assert_allowlist_holds_no_secret_shapes({"OPENAI_API_KEY", "MATRX_ENV"})
    assert "OPENAI_API_KEY" in str(exc.value)
    assert "PUBLIC by design" in str(exc.value)


def test_the_shipped_allowlist_holds_no_secret_shapes():
    from orchestrator.sandbox_manager import _assert_allowlist_holds_no_secret_shapes

    _assert_allowlist_holds_no_secret_shapes()  # must not raise


# ── Every other env-to-container path obeys the same allowlist ──────────────


def test_migration_refresh_strips_the_fourteen_from_an_aidream_box():
    """The other place env reaches a container. A box born before this fix is
    cleaned by its next migration, not left carrying a live provider key."""
    from orchestrator.migrate import _refresh_platform_environment

    existing = [f"{k}={v}" for k, v in HOST_ENV.items()]
    existing.append("USER_CHOSEN_VALUE=keep-me")
    refreshed, _changed = _refresh_platform_environment(
        existing, template="aidream", allow_master_credentials=False,
    )
    names = {item.split("=", 1)[0] for item in refreshed}
    assert sorted(set(THE_FOURTEEN) & names) == []
    assert "USER_CHOSEN_VALUE=keep-me" in refreshed
    assert "MATRX_PLATFORM_AUTH_JWKS_URL" in names


def test_binding_sweep_clears_the_fourteen_from_a_live_aidream_box():
    """The third path: a RUNNING box's shell. The sweep used to skip the aidream
    template wholesale, so every box born before this fix would keep a live
    ANTHROPIC_KEY until it was destroyed."""
    from orchestrator.vault_env_refresh import leaked_platform_names

    leaked = leaked_platform_names(
        sorted(HOST_ENV) + ["BASE_DIR", "SANDBOX_ID", "MY_OWN_VAULT_SECRET"],
        template="aidream",
        vault_names={"MY_OWN_VAULT_SECRET"},
    )
    # 13 of the 14. MATRX_AGENT_TOKEN is the ONE name on that list the
    # orchestrator itself mints PER BOX (``agent_token_for``): in a live box the
    # value is that box's own daemon secret, and unsetting it would break the
    # daemon it authenticates. It is denied from the HOST's env by the allowlist
    # (asserted above at create time) and protected here by
    # ORCHESTRATOR_MANAGED_ENV — the same contract create_sandbox asserts.
    assert set(THE_FOURTEEN) - {"MATRX_AGENT_TOKEN"} <= set(leaked)
    # Never the person's own vault value, the orchestrator's identity vars, or
    # the path overrides the orchestrator itself sets for this template.
    assert "MY_OWN_VAULT_SECRET" not in leaked
    assert "SANDBOX_ID" not in leaked
    assert "MATRX_AGENT_TOKEN" not in leaked
    assert "BASE_DIR" not in leaked
    assert "MATRX_PLATFORM_AUTH_JWKS_URL" not in leaked


def test_agent_env_endpoint_never_returns_a_value():
    """The endpoint that DISCLOSED the leak. It answers 'does the box see X?'
    by name; a value is read from inside the box by its owner."""
    from orchestrator.routes.sandboxes import _kv_list_from_env_lines

    records = _kv_list_from_env_lines(
        [f"ANTHROPIC_KEY={THE_FOURTEEN['ANTHROPIC_KEY']}", "EMPTY="]
    )
    assert records[0]["key"] == "ANTHROPIC_KEY"
    assert records[0]["present"] is True
    assert records[0]["redacted"] is True
    assert "value" not in records[0]
    blob = repr(records)
    assert THE_FOURTEEN["ANTHROPIC_KEY"] not in blob
    assert "sk-ant" not in blob


# ── The operator knob is the one explicit, supervised widening ──────────────


@pytest.mark.asyncio
async def test_knob_on_is_the_only_widening_and_it_says_so(created_env, caplog):
    seed_sandbox_knobs({"aidream_template_forwards_master_credentials": True})
    with caplog.at_level("WARNING"):
        env = await created_env("aidream")
    assert env["ANTHROPIC_KEY"] == THE_FOURTEEN["ANTHROPIC_KEY"]
    assert "aidream_template_forwards_master_credentials" in caplog.text


@pytest.mark.asyncio
async def test_knob_on_still_opens_nothing_for_a_user_template(created_env):
    seed_sandbox_knobs({"aidream_template_forwards_master_credentials": True})
    env = await created_env("slim")
    assert sorted(set(HOST_ENV) & set(env)) == []
