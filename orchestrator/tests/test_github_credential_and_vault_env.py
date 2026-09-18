"""A box's GitHub credential and vault environment are LIVE, not a birthday snapshot.

THE TWO BUGS THESE GUARDS LOCK DOWN (Arman's box sbx-7ddc2eb0c364, 2026-09-18;
root causes confirmed live on admin's box sbx-cd6d53863995 the same day).

1. THE DEAD PAT THAT MASKED THE BRIDGE. ``matrx-git-credential-env`` fell back
   to ``GITHUB_TOKEN -> GH_TOKEN -> GITHUB_PAT -> MATRX_GITHUB_TOKEN`` and then
   ``exit 0`` regardless. Three names in that chain point at credentials the
   CONTAINER was born carrying rather than anything the person chose, and the
   orchestrator's own passthrough registry listed ``GITHUB_PAT`` — the platform
   host's master token. So a bridge failure silently became a push with a
   revoked token, and ``exit 0`` left git to print its own generic
   "authentication failed" over the helper's explanation.

2. THE FROZEN VAULT. The user's injectable vault values are fetched exactly
   once, at container create. Arman deleted his ``GITHUB_PAT`` vault row on
   2026-07-24 and boxes still carried the name on 2026-09-18; ``GITHUB_TOKEN``
   and ``BRIGHT_DATA_API_KEY`` were named in the briefing as present and were
   not. A snapshot cannot be a promise.

Every test here fails against the pre-fix code. They read the SHIPPED shell
scripts and the SHIPPED orchestrator source rather than restating them, so a
regression in the real artifact — not a copy of it — is what turns them red.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "sandbox-image" / "scripts"
CREDENTIAL_HELPER = SCRIPTS / "matrx-git-credential-env"
CONFIGURE = SCRIPTS / "configure-git-credentials.sh"


# ── 1. The dead-PAT class ────────────────────────────────────────────────────


def _code_only(path: Path) -> str:
    """The script with comment lines stripped — so the history we deliberately
    WROTE DOWN in the header cannot satisfy a test about live behaviour."""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_PAT", "MATRX_GITHUB_TOKEN"])
def test_the_helper_never_reads_a_container_born_token(name: str) -> None:
    """Only the person's own GITHUB_TOKEN vault item may stand in for the bridge.

    The three names here were satisfiable by the platform host's credential or
    by a value frozen into the container months ago.
    """
    code = _code_only(CREDENTIAL_HELPER)
    # An EXPANSION of the name is a read. A bare mention inside the "this is
    # set but no longer used" warning loop is not: it names the variable for
    # the person, it never consumes it.
    reads = re.findall(rf"\$\{{{name}[:\-}}]", code) + re.findall(rf"\${name}\b", code)
    assert reads == [], (
        f"{name} is still expanded as a credential in {CREDENTIAL_HELPER.name}; "
        "the fallback chain is GITHUB_TOKEN alone"
    )


def test_the_helper_exits_non_zero_when_it_has_no_credential() -> None:
    """``exit 0`` made git bury the only explanation that existed."""
    code = _code_only(CREDENTIAL_HELPER)
    no_token_block = code[code.index('if [ -z "$token" ]; then', code.index("username=") - 4000):]
    assert "exit 1" in no_token_block, (
        "the no-credential branch must exit non-zero so git surfaces the reason "
        "instead of printing its own generic authentication failure"
    )


def test_the_helper_prints_the_servers_own_words_on_a_refusal() -> None:
    code = _code_only(CREDENTIAL_HELPER)
    assert "$bridge_body" in code and "$bridge_status" in code, (
        "a bridge refusal must reach stderr verbatim — the server knows why, "
        "this script does not"
    )


def test_configure_script_offers_the_same_single_fallback() -> None:
    code = _code_only(CONFIGURE)
    for name in ("GH_TOKEN", "GITHUB_PAT", "MATRX_GITHUB_TOKEN"):
        assert name not in code, (
            f"{CONFIGURE.name} still advertises {name}; the two scripts must agree "
            "on what a box's git credential can be"
        )


def test_github_pat_is_not_on_the_orchestrator_passthrough_list() -> None:
    """The platform host's own PAT must not be a name a container can receive."""
    from orchestrator.config import settings

    names = {n.strip() for n in settings.aidream_passthrough_env.split(",") if n.strip()}
    assert "GITHUB_PAT" not in names, (
        "GITHUB_PAT is back on the passthrough registry — that is the "
        "orchestrator HOST's GitHub account being offered to a container"
    )
    assert "GITHUB_CLIENT_ID" in names, "the sanity half: the registry is still populated"


def test_development_template_requires_the_persons_own_token() -> None:
    """The create gate used to accept the platform PAT as proof of readiness."""
    source = (
        REPO / "orchestrator" / "orchestrator" / "sandbox_manager.py"
    ).read_text()
    anchor = source.index("internal development sandbox requires")
    gate = source[anchor - 1400 : anchor + 400]
    assert 'env.get("GITHUB_TOKEN")' in gate
    for name in ('"GH_TOKEN"', '"GITHUB_PAT"', '"MATRX_GITHUB_TOKEN"'):
        assert f"if not any(env.get({name}" not in gate
    assert "GH_TOKEN, GITHUB_PAT and MATRX_GITHUB_TOKEN are" in gate, (
        "the refusal must say what stopped being accepted, not just fail"
    )


# ── 2. The frozen-vault class ────────────────────────────────────────────────


def _render(env, *, version="v1", removed=None) -> str:
    from orchestrator.vault_env_refresh import render_env_file

    return render_env_file(env, version=version, removed=removed)


def _run(script: str, probe: str, *, preset: dict[str, str] | None = None) -> str:
    """Source the rendered file in a REAL bash with a controlled environment.

    The file's whole job is what a shell ends up holding, so it is executed,
    never pattern-matched.
    """
    env = {"PATH": "/usr/bin:/bin", **(preset or {})}
    out = subprocess.run(
        ["bash", "-c", f"{script}\n{probe}"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_a_value_added_after_the_box_was_born_lands_in_the_shell() -> None:
    script = _render({"SERPAPI_API_KEY": "live-value"})
    assert _run(script, 'echo "$SERPAPI_API_KEY"') == "live-value"


def test_a_value_deleted_from_the_vault_is_unset_not_merely_shadowed() -> None:
    """THE bug: the container's environ still carries a name the vault dropped.

    Exporting the survivors is not enough — the dead name has to be removed,
    or the credential helper keeps finding it.
    """
    script = _render({"GITHUB_TOKEN": "fresh"}, removed=["GITHUB_PAT"])
    result = _run(
        script,
        'echo "PAT=[${GITHUB_PAT-<unset>}] TOKEN=[$GITHUB_TOKEN]"',
        preset={"GITHUB_PAT": "revoked-two-months-ago"},
    )
    assert result == "PAT=[<unset>] TOKEN=[fresh]"


def test_a_rotated_value_beats_the_containers_birthday_value() -> None:
    script = _render({"BRIGHT_DATA_API_KEY": "rotated"})
    assert (
        _run(
            script,
            'echo "$BRIGHT_DATA_API_KEY"',
            preset={"BRIGHT_DATA_API_KEY": "born-with-this"},
        )
        == "rotated"
    )


def test_values_with_shell_metacharacters_survive_intact() -> None:
    """A secret is arbitrary bytes. Quoting is not a detail here: a mangled
    token fails at the provider with a message nobody can trace back to this."""
    nasty = "a b'c\"d$e`f\\g;h|i\nj"
    script = _render({"WEIRD": nasty})
    assert _run(script, 'printf "%s" "$WEIRD"') == nasty.replace("\n", "\n")


def test_a_caller_supplied_env_var_is_never_overwritten_by_the_vault() -> None:
    """``exec_in_sandbox(env=...)`` is a deliberate per-call value. The vault
    outranks the stale container snapshot; it must not outrank a caller."""
    script = _render({"MODE": "from-vault"})
    result = _run(
        script,
        'echo "$MODE"',
        preset={"MODE": "from-caller", "MATRX_VAULT_ENV_SKIP": "MODE"},
    )
    assert result == "from-caller"


def test_the_rendered_file_leaves_no_helper_functions_behind() -> None:
    """It is sourced into the agent's own shell; it may not pollute it."""
    script = _render({"A": "1"})
    assert _run(script, 'type -t __matrx_vault_set || echo "gone"') == "gone"


def test_the_version_changes_when_a_value_rotates_not_only_when_names_do() -> None:
    """The stamp is the rate limiter. If it keyed on names alone, a rotated
    secret would never be delivered."""
    from orchestrator.vault_env_refresh import vault_version

    before = vault_version({"K": "old"})
    assert before == vault_version({"K": "old"}), "must be stable"
    assert before != vault_version({"K": "new"}), "a rotation must invalidate the stamp"
    assert before != vault_version({"K": "old", "J": "x"}), "a new name must too"


def test_the_stamp_never_contains_a_secret_value() -> None:
    from orchestrator.vault_env_refresh import vault_version

    assert "super-secret" not in vault_version({"K": "super-secret"})


def test_the_exec_wrapper_sources_the_vault_file() -> None:
    """``docker exec`` runs ``bash -c`` — no profile, no bashrc. Without this
    line the refresh would reach ssh sessions and miss the agent's tool path,
    which is the shell that actually runs the work."""
    source = (REPO / "orchestrator" / "orchestrator" / "sandbox_manager.py").read_text()
    wrapper = source[source.index("        vault_line = ") :][:1200]
    assert "VAULT_ENV_FILE" in wrapper
    assert "BRIDGE_ENV_FILE" in wrapper, (
        "the identity belongs to the tool path too: on admin's sbx-cd6d53863995 "
        "(2026-09-18) the login shell had ORGANIZATION_ID and `docker exec` did "
        "not, because only the profile drop-in published it"
    )
    assert wrapper.index("BRIDGE_ENV_FILE") < wrapper.index("VAULT_ENV_FILE"), (
        "identity is sourced first so a person's own vault value of the same "
        "name still wins in their shell"
    )
    assert "MATRX_VAULT_ENV_SKIP" in wrapper, (
        "the wrapper must tell the file which names this exec set on purpose"
    )


def test_the_refresh_never_publishes_platform_env_into_a_box() -> None:
    """CLAUDE.md: never add a third env source without this guard covering it.

    The vault refresh IS a new env source. It is lawful because its ONLY input
    is the person's own vault, read through aidream — never the orchestrator's
    process environment. This test fails the moment that stops being true.
    """
    source = (
        REPO / "orchestrator" / "orchestrator" / "vault_env_refresh.py"
    ).read_text()
    assert "os.environ" not in source, (
        "the vault refresh read the orchestrator's own environment — that is "
        "the 2026-09-13 platform-env leak reached from a new direction"
    )
    assert "platform_passthrough_env" not in source
    assert "sandbox-env-for-user" in source, (
        "the one lawful source is aidream's per-user vault route"
    )


def test_a_fetch_failure_never_empties_the_box() -> None:
    """If aidream is unreachable, "no values" must not be read as "the vault is
    empty" — that would unset every credential the box legitimately holds."""
    source = (
        REPO / "orchestrator" / "orchestrator" / "vault_env_refresh.py"
    ).read_text()
    body = source[source.index("    env, fetch_error = await _fetch_vault_env") :][:800]
    assert "return _result(\"unavailable\"" in body
    assert "_write_env_files" not in body


# ── 3. Old boxes get the scripts, not just the SDK ───────────────────────────


def test_the_binding_refresh_delivers_scripts_and_reinstalls_what_changed() -> None:
    """Admin's box, live on 2026-09-18: /opt/sandbox/sdk dated Sep 18 beside
    /opt/sandbox/scripts dated Aug 15, holding a 925-byte credential helper
    that had never heard of the AI Dream bridge."""
    from orchestrator import sdk_refresh

    assert sdk_refresh.SCRIPTS_PATH == "/opt/sandbox/scripts"
    reinstalled = {name for name, _cmd, _why in sdk_refresh.SCRIPT_REINSTALL}
    assert "matrx-git-credential-env" in reinstalled, (
        "delivering the helper without re-running configure-git-credentials.sh "
        "leaves ~/.gitconfig pointing at the old arrangement"
    )
    assert "write-bridge-env.sh" in reinstalled, (
        "a box with no /etc/matrx must gain one; every shell's identity depends "
        "on that file existing"
    )


def test_the_scripts_stamp_is_contract_versioned() -> None:
    """A box the PREVIOUS version of this code already stamped must still be
    reached once — otherwise every box that ever got an SDK refresh keeps its
    birthday scripts forever."""
    from orchestrator import sdk_refresh

    assert isinstance(sdk_refresh.SCRIPTS_CONTRACT, int)
    source = (REPO / "orchestrator" / "orchestrator" / "sdk_refresh.py").read_text()
    assert 'stamp.get("contract") == SCRIPTS_CONTRACT' in source


def test_the_scripts_half_runs_even_when_the_sdk_is_already_current() -> None:
    source = (REPO / "orchestrator" / "orchestrator" / "sdk_refresh.py").read_text()
    body = source[source.index("        async with activity.track(sandbox_id):") :][:1600]
    assert body.index("_refresh_scripts") < body.index('stamp.get("to_image_id")'), (
        "the scripts refresh must precede the SDK's already_refreshed "
        "short-circuit, or a box whose SDK is current never gets new scripts"
    )


def test_the_staged_tree_can_never_land_outside_the_staging_directory() -> None:
    """Same guarantee the SDK half had: the tar is rewritten, not trusted."""
    import io
    import tarfile

    from orchestrator import sdk_refresh

    src = io.BytesIO()
    with tarfile.open(fileobj=src, mode="w") as archive:
        for name in ("scripts/ok.sh", "scripts/../../etc/passwd"):
            info = tarfile.TarInfo(name=name)
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))
    src.seek(0)

    class _Holder:
        def get_archive(self, path):
            return [src.getvalue()], {}

        def remove(self, force=False):
            return None

    class _Client:
        containers = type("C", (), {"create": staticmethod(lambda *a, **k: _Holder())})()

    payload = sdk_refresh._stage_payload(
        _Client(), "tag", sdk_refresh.SCRIPTS_PATH, sdk_refresh.SCRIPTS_STAGE_DIR
    )
    with tarfile.open(fileobj=io.BytesIO(payload)) as out:
        for member in out.getmembers():
            assert member.name.split("/")[0] == sdk_refresh.SCRIPTS_STAGE_DIR, member.name


# ── 4. The binding reports all of it ─────────────────────────────────────────


def test_the_binding_response_carries_the_vault_report() -> None:
    source = (REPO / "orchestrator" / "orchestrator" / "routes" / "sandboxes.py").read_text()
    prepare = source[source.index("async def _prepare_connection") :][:4000]
    assert "refresh_vault_env" in prepare
    assert prepare.count('"vault_env_refresh"') >= 3, (
        "every return path of the hook must report it — a missing key on the "
        "failure path is exactly how a silent refresh failure hides"
    )


def test_the_report_names_what_changed_and_never_a_value() -> None:
    from orchestrator.vault_env_refresh import _result

    report = _result("refreshed", added=["SERPAPI_API_KEY"], removed=["GITHUB_PAT"])
    assert report["added"] == ["SERPAPI_API_KEY"]
    assert report["removed"] == ["GITHUB_PAT"]
    assert json.dumps(report).count("=") == 0, "a report is names, never assignments"


# ── 5. An old box regains its identity without recreation ────────────────────


def test_the_identity_comes_from_the_orchestrator_never_the_container() -> None:
    """A box old enough to miss ``write-bridge-env.sh`` has no /etc/matrx at all,
    and admin's sbx-cd6d53863995 (read live 2026-09-18) had no ORGANIZATION_ID
    anywhere. Reading the identity out of the container env would reproduce
    exactly the hole we are filling."""
    from orchestrator.vault_env_refresh import box_identity

    class _Sandbox:
        sandbox_id = "sbx-cd6d53863995"
        user_id = "87a6e699-3622-4869-8843-d0867456c0dd"
        organization_id = "884d1ce8-7b49-4fba-a2f3-0f7dd7c83d4f"

    identity = box_identity(_Sandbox())
    assert identity["USER_ID"] == _Sandbox.user_id
    assert identity["ORGANIZATION_ID"] == _Sandbox.organization_id, (
        "the organization is the half bridge-headers.sh refuses without, and "
        "the half an old container never had"
    )


def test_the_identity_file_is_what_a_shell_can_source() -> None:
    from orchestrator.vault_env_refresh import render_identity_file

    rendered = render_identity_file(
        {"USER_ID": "u-1", "ORGANIZATION_ID": "o 1", "MATRX_AIDREAM_URL": "https://x"}
    )
    assert _run(rendered, 'echo "$USER_ID|$ORGANIZATION_ID|$MATRX_AIDREAM_URL"') == (
        "u-1|o 1|https://x"
    )


def test_an_absent_identity_value_is_never_exported_as_empty() -> None:
    """``ORGANIZATION_ID=""`` passes bridge-headers.sh's presence check and then
    fails at AI Dream with a 400 nobody can read back to here."""
    from orchestrator.vault_env_refresh import box_identity

    class _Sandbox:
        sandbox_id = "sbx-x"
        user_id = "u-1"
        organization_id = ""

    assert "ORGANIZATION_ID" not in box_identity(_Sandbox())


def test_a_stale_failed_marker_is_cleared_once_the_box_is_wired() -> None:
    """bridge-headers.sh screams while /etc/matrx/bridge-env.FAILED exists. If
    the refresh wires the box and leaves the marker, every shell keeps saying
    the box is broken."""
    source = (
        REPO / "orchestrator" / "orchestrator" / "vault_env_refresh.py"
    ).read_text()
    assert "rm -f {BRIDGE_FAILED_FILE}" in source


def test_the_refresh_writes_the_same_paths_write_bridge_env_owns() -> None:
    """Two files with two opinions about a box's identity is worse than one
    stale file. The republish targets the SAME paths the boot script writes."""
    from orchestrator import vault_env_refresh as v

    writer = (SCRIPTS / "write-bridge-env.sh").read_text()
    assert f'BRIDGE_ENV_FILE:-$BRIDGE_ENV_DIR/bridge-env.sh' in writer
    assert v.BRIDGE_ENV_FILE == "/etc/matrx/bridge-env.sh"
    assert "00-matrx-bridge.sh" in writer and v.BRIDGE_PROFILE_DROPIN.endswith(
        "00-matrx-bridge.sh"
    )


# ── 6. Boxes created before the platform-env leak fix get cleaned ────────────
#
# Incident 2026-09-13 stopped the passthrough reaching non-`aidream` templates
# for boxes created AFTERWARDS. Nothing cleaned the ones already running. Admin's
# hosted sbx-5515667942bd (created 2026-08-17), read live on 2026-09-18, still
# held GITHUB_PAT, GITHUB_CLIENT_SECRET, GITHUB_CLIENT_ID and the bot identity.


def _leaked(names, *, template="bare", vault=()):
    from orchestrator.vault_env_refresh import leaked_platform_names

    return leaked_platform_names(list(names), template=template, vault_names=set(vault))


def test_the_platform_credentials_a_pre_fix_box_still_holds_are_cleared() -> None:
    """The exact name set read off admin's hosted box on 2026-09-18."""
    observed = [
        "GITHUB_PAT",
        "GITHUB_CLIENT_SECRET",
        "GITHUB_CLIENT_ID",
        "GITHUB_BOT_ACCOUNT_USERNAME",
        "GITHUB_BOT_EMAIL",
        "GITHUB_ORG_NAME",
        "SANDBOX_ID",
        "USER_ID",
    ]
    cleared = _leaked(observed)

    assert "GITHUB_PAT" in cleared
    assert "GITHUB_CLIENT_SECRET" in cleared, (
        "a platform OAuth client secret in a user's box is the thing the "
        "incident was about"
    )


def test_the_orchestrator_s_own_env_is_never_cleared() -> None:
    """The one way this sweep could break a live box. MATRX_AIDREAM_SERVICE_TOKEN
    and AWS_SECRET_ACCESS_KEY both MATCH the master-credential patterns and are
    both put there by the orchestrator on purpose; a hosted box loses its S3
    sync without the second."""
    from orchestrator.sandbox_manager import ORCHESTRATOR_MANAGED_ENV

    cleared = _leaked(sorted(ORCHESTRATOR_MANAGED_ENV))

    assert cleared == [], f"the sweep would have broken a live box: {cleared}"


def test_the_protected_set_cannot_drift_from_what_create_actually_sets() -> None:
    """A managed name missing from the protected set would be cleared out of
    every live box. create_sandbox refuses at CREATE rather than letting that
    reach the sweep."""
    source = (REPO / "orchestrator" / "orchestrator" / "sandbox_manager.py").read_text()
    assert "ORCHESTRATOR_MANAGED_ENV" in source
    assert "_unregistered = sorted(set(env) - ORCHESTRATOR_MANAGED_ENV)" in source
    assert "binding-time leak sweep will clear them out of live boxes" in source


def test_a_persons_own_vault_value_is_never_treated_as_a_leak() -> None:
    """Somebody's own GITHUB_PAT vault item, marked inject, is theirs."""
    assert _leaked(["GITHUB_PAT"], vault=["GITHUB_PAT"]) == []
    assert _leaked(["OPENAI_API_KEY"], vault=["OPENAI_API_KEY"]) == []


def test_the_aidream_template_keeps_the_platform_env_it_is_entitled_to() -> None:
    """That template exists to run aidream itself inside a box. Sweeping it
    would break the one lawful consumer."""
    assert _leaked(["GITHUB_PAT", "SUPABASE_MATRIX_PASSWORD"], template="aidream") == []


def test_an_ordinary_non_credential_name_is_left_alone() -> None:
    """The sweep is not a general env cleaner. A box's own PATH, HOME, or a
    tool's plain setting is none of its business."""
    assert _leaked(["PATH", "HOME", "LANG", "NODE_ENV", "GITHUB_ORG_NAME"]) == []


def test_the_cleared_names_are_reported_and_actually_unset() -> None:
    """Reporting a clear that did not happen is worse than not clearing."""
    from orchestrator.vault_env_refresh import render_env_file

    script = render_env_file({"SERPAPI_API_KEY": "live"}, version="v", removed=["GITHUB_PAT"])
    assert (
        _run(
            script,
            'echo "${GITHUB_PAT-<unset>}"',
            preset={"GITHUB_PAT": "the-platform-token"},
        )
        == "<unset>"
    )


def test_the_sweep_rides_its_own_knob() -> None:
    from orchestrator import vault_env_refresh as v

    assert v.LEAK_KNOB == "clear_leaked_platform_env_on_binding"
    source = (REPO / "orchestrator" / "orchestrator" / "vault_env_refresh.py").read_text()
    assert "await knob_bool(LEAK_KNOB)" in source
    assert "LEAK SWEEP UNAVAILABLE" in source, (
        "a missing knob row must say so rather than silently not sweeping"
    )
