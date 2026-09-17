from __future__ import annotations

import os
import subprocess
from pathlib import Path


HELPER = Path(__file__).resolve().parents[2] / "scripts" / "matrx-git-credential-env"


def _run_helper(stdin: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    for key in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_PAT",
        "MATRX_GITHUB_TOKEN",
        "MATRX_AIDREAM_URL",
        "MATRX_AIDREAM_SERVICE_TOKEN",
        "USER_ID",
        "ORGANIZATION_ID",
    ):
        merged_env.pop(key, None)
    # Hermetic by default: the published identity file only exists when a test
    # writes one (a developer box running these tests has no /etc/matrx).
    merged_env["MATRX_BRIDGE_ENV_FILE"] = merged_env.get(
        "MATRX_BRIDGE_ENV_FILE", "/nonexistent/matrx/bridge-env.sh"
    )
    if env:
        merged_env.update(env)
    return subprocess.run(
        [str(HELPER), "get"],
        input=stdin,
        text=True,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _matching_curl(tmp_path: Path, pattern_body: str) -> Path:
    """A curl stub shaped like the real one.

    The script asks for ``-o <file> -w '%{http_code}'``, so the BODY goes to
    the file and only the STATUS reaches stdout. A stub that printed the token
    on stdout would be testing a transport we do not use.
    """
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "out=''; prev=''\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$prev\" = '-o' ]; then out=\"$arg\"; fi\n"
        "  prev=\"$arg\"\n"
        "done\n"
        "emit() { if [ -n \"$out\" ]; then printf '%s' \"$1\" > \"$out\"; "
        "else printf '%s' \"$1\"; fi; printf '%s' \"$2\"; }\n"
        + pattern_body
    )
    fake_curl.chmod(0o755)
    return fake_curl


def test_env_helper_returns_github_token():
    proc = _run_helper(
        "protocol=https\nhost=github.com\n\n",
        {"GITHUB_PAT": "ghp_secret", "GITHUB_USERNAME": "octo"},
    )

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout == "username=octo\npassword=ghp_secret\n"


def test_env_helper_uses_standard_default_username():
    proc = _run_helper("protocol=https\nhost=github.com\n\n", {"GH_TOKEN": "ghs_secret"})

    assert proc.returncode == 0
    assert proc.stdout == "username=x-access-token\npassword=ghs_secret\n"


def test_env_helper_ignores_non_github_hosts():
    proc = _run_helper(
        "protocol=https\nhost=gitlab.com\n\n",
        {"GITHUB_TOKEN": "ghp_secret"},
    )

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_env_helper_noops_without_token():
    """No identity and no token: git gets nothing — but it SAYS so, because
    git itself only reports "authentication failed" and the user is left
    guessing whether their connected GitHub account is missing or broken."""
    proc = _run_helper("protocol=https\nhost=github.com\n\n")

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "no AI Dream identity" in proc.stderr


def test_helper_reads_the_published_identity_when_the_shell_has_none(tmp_path: Path):
    """THE SSH CASE. sshd passes the container environment to nothing, so this
    helper ran with no URL, no token, no USER_ID and no ORGANIZATION_ID on a
    box that was fully wired — bridge-headers.sh took its QUIET "unwired image"
    branch and git silently pushed with no credential. The entrypoint now
    publishes the identity to /etc/matrx/bridge-env.sh and the helper reads it.

    The fake curl answers only when all three identity headers are present, so
    a helper that fell back to "unwired" would return nothing here."""
    identity = tmp_path / "bridge-env.sh"
    identity.write_text(
        'export USER_ID="user-123"\n'
        'export ORGANIZATION_ID="org-9"\n'
        'export MATRX_AIDREAM_URL="https://server.example.test"\n'
        'export MATRX_AIDREAM_SERVICE_TOKEN="bridge-secret"\n'
    )
    _matching_curl(
        tmp_path,
        "case \"$*\" in\n"
        "  *'Bearer bridge-secret'*'X-Matrx-User-Id: user-123'*'X-Organization-Id: org-9'*)"
        " emit ghu_fresh 200 ;;\n"
        "  *) emit '' 401 ;;\n"
        "esac\n",
    )

    proc = _run_helper(
        "protocol=https\nhost=github.com\n\n",
        {
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "MATRX_BRIDGE_ENV_FILE": str(identity),
        },
    )

    assert proc.returncode == 0
    assert proc.stdout == "username=x-access-token\npassword=ghu_fresh\n"
    assert proc.stderr == ""


def test_published_identity_never_overrides_the_process_environment(tmp_path: Path):
    """The file is a fallback for shells sshd starved, never an override: a
    container whose env names one organization must not have a stale file
    silently switch the tenant its writes land in."""
    identity = tmp_path / "bridge-env.sh"
    identity.write_text(
        'export USER_ID="stale-user"\n'
        'export ORGANIZATION_ID="stale-org"\n'
        'export MATRX_AIDREAM_URL="https://stale.example.test"\n'
        'export MATRX_AIDREAM_SERVICE_TOKEN="stale-secret"\n'
    )
    _matching_curl(
        tmp_path,
        "case \"$*\" in\n"
        "  *'X-Matrx-User-Id: live-user'*'X-Organization-Id: live-org'*) emit ghu_live 200 ;;\n"
        "  *) emit ghu_stale 200 ;;\n"
        "esac\n",
    )

    proc = _run_helper(
        "protocol=https\nhost=github.com\n\n",
        {
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "MATRX_BRIDGE_ENV_FILE": str(identity),
            "MATRX_AIDREAM_URL": "https://live.example.test",
            "MATRX_AIDREAM_SERVICE_TOKEN": "live-secret",
            "USER_ID": "live-user",
            "ORGANIZATION_ID": "live-org",
        },
    )

    assert proc.stdout == "username=x-access-token\npassword=ghu_live\n"


def test_partial_identity_is_loud_even_when_the_file_is_absent(tmp_path: Path):
    """A wired box missing ONE variable is a defect and says which. The quiet
    branch is for a box carrying no identity at all — nothing else."""
    proc = _run_helper(
        "protocol=https\nhost=github.com\n\n",
        {"USER_ID": "user-123"},
    )

    assert proc.returncode == 0
    assert "MATRX_AIDREAM_URL" in proc.stderr
    assert "ORGANIZATION_ID" in proc.stderr


def test_helper_prefers_refreshable_aimatrx_connection(tmp_path: Path):
    """The bridge call carries BOTH halves of the request context; the fake
    curl refuses to answer unless it sees the organization header too."""
    _matching_curl(
        tmp_path,
        "case \"$*\" in\n"
        "  *'X-Organization-Id: org-9'*'/api/github-integrations/internal/access-token'*)"
        " emit ghu_fresh 200 ;;\n"
        "  *) emit '' 401 ;;\n"
        "esac\n",
    )
    proc = _run_helper(
        "protocol=https\nhost=github.com\n\n",
        {
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "MATRX_AIDREAM_URL": "https://server.example.test",
            "MATRX_AIDREAM_SERVICE_TOKEN": "bridge-secret",
            "USER_ID": "user-123",
            "ORGANIZATION_ID": "org-9",
            "GITHUB_PAT": "ghp_stale-fallback",
        },
    )

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout == "username=x-access-token\npassword=ghu_fresh\n"


def test_helper_falls_back_to_injected_token_when_bridge_is_unavailable(tmp_path: Path):
    fake_curl = tmp_path / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nexit 22\n")
    fake_curl.chmod(0o755)
    proc = _run_helper(
        "protocol=https\nhost=github.com\n\n",
        {
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "MATRX_AIDREAM_URL": "https://server.example.test",
            "MATRX_AIDREAM_SERVICE_TOKEN": "bridge-secret",
            "USER_ID": "user-123",
            "ORGANIZATION_ID": "org-9",
            "GH_TOKEN": "ghp_fallback",
        },
    )

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout == "username=x-access-token\npassword=ghp_fallback\n"


def test_helper_refuses_the_bridge_call_without_an_organization(tmp_path: Path):
    """A wired box missing ORGANIZATION_ID must NOT call AI Dream with half the
    request context — it says which variable is missing and skips the bridge.

    Before 2026-09-17 this call went out with the user only, and every write it
    caused on the other side landed in whatever organization the code below
    defaulted to. The fake curl here answers ANY request, so a silent omission
    would show up as ``ghu_fresh``."""
    _matching_curl(tmp_path, "emit ghu_fresh 200\n")
    proc = _run_helper(
        "protocol=https\nhost=github.com\n\n",
        {
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "MATRX_AIDREAM_URL": "https://server.example.test",
            "MATRX_AIDREAM_SERVICE_TOKEN": "bridge-secret",
            "USER_ID": "user-123",
            "GH_TOKEN": "ghp_fallback",
        },
    )

    assert proc.returncode == 0
    assert "ORGANIZATION_ID" in proc.stderr
    # Fell back to the injected token instead of a wrong-tenant bridge call.
    assert proc.stdout == "username=x-access-token\npassword=ghp_fallback\n"


# ── The server's refusal is the server's words ──────────────────────────────
# Until 2026-09-17 the bridge call was `curl -fsS … 2>/dev/null || true`, which
# throws away the status AND the body. Every refusal — including the two the
# organization-scoped bridge now returns, 403 organization_membership_required
# and 503 membership_unverifiable — therefore landed in the "Connect a GitHub
# account" branch: a remedy the person cannot act on for a problem they do not
# have.


def _identity_env(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "MATRX_AIDREAM_URL": "https://server.example.test",
        "MATRX_AIDREAM_SERVICE_TOKEN": "bridge-secret",
        "USER_ID": "user-123",
        "ORGANIZATION_ID": "org-9",
    }


def _stub_curl(tmp_path: Path, status: str, body: str) -> None:
    """One fixed answer from the bridge: ``body`` with HTTP ``status``."""
    _matching_curl(tmp_path, f"emit {body!r} {status}\n")


def test_a_membership_refusal_prints_the_servers_message_and_remedy(tmp_path: Path):
    _stub_curl(
        tmp_path,
        "403",
        '{"detail":{"code":"organization_membership_required",'
        '"message":"user-123 is not a member of org-9",'
        '"remedy":"Ask an admin of that organization to add you."}}',
    )

    proc = _run_helper("protocol=https\nhost=github.com\n\n", _identity_env(tmp_path))

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "403" in proc.stderr
    assert "organization_membership_required" in proc.stderr
    assert "not a member of org-9" in proc.stderr
    assert "Ask an admin of that organization to add you." in proc.stderr
    # The WRONG remedy must not appear.
    assert "Connect a GitHub account" not in proc.stderr


def test_an_unverifiable_membership_is_reported_as_the_server_stated_it(tmp_path: Path):
    _stub_curl(
        tmp_path,
        "503",
        '{"detail":{"code":"membership_unverifiable",'
        '"message":"membership lookup is unavailable",'
        '"remedy":"Try again in a few minutes."}}',
    )

    proc = _run_helper("protocol=https\nhost=github.com\n\n", _identity_env(tmp_path))

    assert "membership_unverifiable" in proc.stderr
    assert "Try again in a few minutes." in proc.stderr
    assert "Connect a GitHub account" not in proc.stderr


def test_a_404_still_means_connect_a_github_account(tmp_path: Path):
    """The pre-existing case is kept: the endpoint answering 'no connection'
    is the one thing 'Connect a GitHub account' is the right remedy for."""
    _stub_curl(tmp_path, "404", '{"detail":"no github integration"}')

    proc = _run_helper("protocol=https\nhost=github.com\n\n", _identity_env(tmp_path))

    assert "Connect a GitHub account" in proc.stderr


def test_a_2xx_body_is_still_the_token(tmp_path: Path):
    """Positive control for the -o/-w rewrite: the happy path is unchanged."""
    _stub_curl(tmp_path, "200", "ghu_fresh")

    proc = _run_helper("protocol=https\nhost=github.com\n\n", _identity_env(tmp_path))

    assert proc.stdout == "username=x-access-token\npassword=ghu_fresh\n"
    assert proc.stderr == ""


def test_a_refusal_still_prefers_an_explicit_token_when_one_exists(tmp_path: Path):
    _stub_curl(tmp_path, "403", '{"detail":{"code":"organization_membership_required"}}')
    env = _identity_env(tmp_path)
    env["GITHUB_PAT"] = "ghp_local"

    proc = _run_helper("protocol=https\nhost=github.com\n\n", env)

    assert proc.stdout == "username=x-access-token\npassword=ghp_local\n"
