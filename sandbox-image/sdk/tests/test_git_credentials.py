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
    ):
        merged_env.pop(key, None)
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
    proc = _run_helper("protocol=https\nhost=github.com\n\n")

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_helper_prefers_refreshable_aimatrx_connection(tmp_path: Path):
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *'/api/github-integrations/internal/access-token'*) printf 'ghu_fresh' ;;\n"
        "  *) exit 22 ;;\n"
        "esac\n"
    )
    fake_curl.chmod(0o755)
    proc = _run_helper(
        "protocol=https\nhost=github.com\n\n",
        {
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "MATRX_AIDREAM_URL": "https://server.example.test",
            "MATRX_AIDREAM_SERVICE_TOKEN": "bridge-secret",
            "USER_ID": "user-123",
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
            "GH_TOKEN": "ghp_fallback",
        },
    )

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout == "username=x-access-token\npassword=ghp_fallback\n"
