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


#: The names V-XT-10 proved the OLD guard let onto the allowlist, because that
#: guard was itself the pattern denylist: none of these matches a pattern, and
#: they are the exact names from the incident.
NAMES_A_DENYLIST_GUARD_MISSES = [
    "ANTHROPIC_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "OPENAI_KEY",
    "MATRX_AGENT_TOKEN",
    "SUPABASE_MATRIX_KEY",
    "TENSORDOCK_AUTH_KEY",
    "HUGGING_FACE_TOKEN_ID",
]


@pytest.mark.parametrize("name", NAMES_A_DENYLIST_GUARD_MISSES)
def test_the_secondary_guard_is_positive_not_a_denylist(name):
    """RED on the first fix: its guard was `is_master_credential_name`, so it
    did not fire for ANY of these — the very names that leaked. The guard is now
    a POSITIVE rule (PLATFORM_ENV_PUBLIC_BASICS, or *_URL with a bare http(s)
    value), so a name earns its place instead of merely dodging a pattern."""
    from orchestrator.sandbox_manager import (
        PLATFORM_ENV_ALLOWLIST,
        _assert_allowlist_is_public_by_design,
    )

    widened = set(PLATFORM_ENV_ALLOWLIST) | {name}
    with pytest.raises(RuntimeError) as exc:
        _assert_allowlist_is_public_by_design(widened, {name: "x"})
    assert name in str(exc.value)
    assert "public by design" in str(exc.value)


def test_the_guard_reads_the_hosts_value_not_just_the_name():
    """A secret can be spelled innocently — ANTHROPIC_KEY carried sk-ant-api03.
    So an allowed NAME whose host VALUE is a credential refuses too."""
    from orchestrator.sandbox_manager import _assert_allowlist_is_public_by_design

    with pytest.raises(RuntimeError) as exc:
        _assert_allowlist_is_public_by_design(
            {"MATRX_ENV"}, {"MATRX_ENV": "sk-ant-api03-not-a-real-key"}
        )
    assert "credential" in str(exc.value)
    # ...and a name that only LOOKS like a public document address.
    with pytest.raises(RuntimeError):
        _assert_allowlist_is_public_by_design(
            {"SNEAKY_URL"}, {"SNEAKY_URL": "postgresql://u:pw@host/db"}
        )


def test_the_guard_reads_the_LIVE_allowlist_not_a_def_time_default(monkeypatch):
    """RED on the first fix: the runtime re-check called the guard with NO
    argument, which bound the def-time default, so widening the module global
    forwarded ADMIN_API_TOKEN's value without raising (V-XT-10). The decision
    must refuse against the allowlist as it is AT CALL TIME."""
    from orchestrator import sandbox_manager

    monkeypatch.setattr(
        sandbox_manager,
        "PLATFORM_ENV_ALLOWLIST",
        frozenset(set(sandbox_manager.PLATFORM_ENV_ALLOWLIST) | {"ADMIN_API_TOKEN"}),
    )
    with pytest.raises(RuntimeError) as exc:
        sandbox_manager.platform_env_decision(
            "aidream",
            allow_master_credentials=False,
            environ={"ADMIN_API_TOKEN": "the-platform-admin-token"},
        )
    assert "ADMIN_API_TOKEN" in str(exc.value)


def test_the_shipped_allowlist_is_public_by_design():
    from orchestrator.sandbox_manager import _assert_allowlist_is_public_by_design

    _assert_allowlist_is_public_by_design()  # must not raise

    # And every entry is either a documented basic or a *_URL — no third way.
    from orchestrator.sandbox_manager import (
        PLATFORM_ENV_ALLOWLIST,
        PLATFORM_ENV_PUBLIC_BASICS,
    )
    unexplained = sorted(
        n for n in PLATFORM_ENV_ALLOWLIST
        if n not in PLATFORM_ENV_PUBLIC_BASICS and not n.endswith("_URL")
    )
    assert unexplained == []


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
    from orchestrator.vault_env_refresh import unentitled_platform_env_names

    leaked = unentitled_platform_env_names(
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


def test_the_sweep_uses_the_allowlist_on_a_NON_passthrough_box_too():
    """THE V-XT-10 FINDING, as a test.

    The first fix converted only the PASSTHROUGH branch to the allowlist and
    left the non-passthrough branch as the pattern denylist — and
    non-passthrough boxes (``bare``, ``slim``) are exactly what the 2026-09-13
    incident contaminated. Run against admin's real running ``bare`` box
    sbx-7520dde5030e, that branch cleared 67 names and LEFT these ten behind.
    RED on the first fix; green on one rule for both branches.
    """
    from orchestrator.vault_env_refresh import unentitled_platform_env_names

    left_behind_by_the_denylist = [
        "ANTHROPIC_KEY", "MATRX_SCRAPER_TOKEN", "SUPABASE_KEY",
        "SUPABASE_MATRIX_KEY", "SUPABASE_DJANGO_KEY", "SUPABASE_AI_MATRIX_KEY",
        "SUPABASE_MATRIX_DJANGO_KEY", "SUPABASE_SAMPLE_MATRIX_KEY",
        "TENSORDOCK_AUTH_KEY", "HUGGING_FACE_TOKEN_ID",
    ]
    for template in ("bare", "slim", "development", None):
        cleared = set(unentitled_platform_env_names(
            sorted(HOST_ENV) + ["PATH", "HOME", "UV_PYTHON", "MY_OWN_VAULT_SECRET"],
            template=template,
            vault_names={"MY_OWN_VAULT_SECRET"},
        ))
        missed = [n for n in left_behind_by_the_denylist if n not in cleared]
        assert missed == [], f"template={template} still leaves {missed}"
        # Never the person's own item, the shell's basics, or the image's vars.
        assert "MY_OWN_VAULT_SECRET" not in cleared
        assert "PATH" not in cleared
        assert "HOME" not in cleared
        assert "UV_PYTHON" not in cleared


def test_one_census_serves_both_doors_and_they_cannot_disagree():
    """/diagnostics reported seven "leaks" on clean boxes because its local
    expression subtracted neither the person's vault nor ORCHESTRATOR_MANAGED_ENV,
    while the sweep subtracted both (V-XT-10). There is one function now, and
    the route calls it — asserted by reading the route's source, because a
    second local expression is exactly the regression to catch."""
    import inspect

    from orchestrator.routes import sandboxes as routes
    from orchestrator.vault_env_refresh import unentitled_platform_env_names

    source = inspect.getsource(routes.sandbox_diagnostics)
    assert "unentitled_platform_env_names" in source
    assert "recorded_vault_names" in source

    # A clean post-fix box: the orchestrator's own vars + the person's vault +
    # the allowlist. Zero findings — no crying wolf.
    clean = [
        "SANDBOX_ID", "USER_ID", "ORGANIZATION_ID", "MATRX_AGENT_TOKEN",
        "MATRX_AIDREAM_URL", "MATRX_AIDREAM_SERVICE_TOKEN",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
        "MATRX_BROWSER_PROFILE_ID", "MATRX_BROWSER_EXECUTION_TARGET",
        "PATH", "LANG", "DEBUG", "LOG_LEVEL", "MATRX_PLATFORM_AUTH_JWKS_URL",
        "BRAVE_SEARCH_API_KEY", "DATA_FOR_SEO_PASSWORD", "SERPAPI_API_KEY",
    ]
    assert unentitled_platform_env_names(
        clean,
        template="aidream",
        vault_names={"BRAVE_SEARCH_API_KEY", "DATA_FOR_SEO_PASSWORD", "SERPAPI_API_KEY"},
    ) == []


def test_recorded_vault_names_reads_the_row_the_create_path_stamps():
    from orchestrator.vault_env_refresh import recorded_vault_names

    class Row:
        config = {"secrets_injection": {"names": ["BRAVE_SEARCH_API_KEY"]},
                  "vault_env_refresh": {"present": ["SERPAPI_API_KEY"]}}
    assert recorded_vault_names(Row()) == {"BRAVE_SEARCH_API_KEY", "SERPAPI_API_KEY"}

    class Old:
        config = None
    assert recorded_vault_names(Old()) == set()


def test_a_failed_env_command_never_returns_its_output():
    """routes/sandboxes.py handed the raw `env` text back as runtime_env_error on
    a non-zero exit — the same disclosure, through the error field (V-XT-10)."""
    import inspect

    from orchestrator.routes import sandboxes as routes

    source = inspect.getsource(routes.sandbox_agent_env)
    assert 'out["runtime_env_error"] = text' not in source
    assert "is withheld" in source


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
