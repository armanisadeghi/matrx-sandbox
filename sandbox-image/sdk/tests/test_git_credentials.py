from __future__ import annotations

import os
import subprocess
from pathlib import Path


HELPER = Path(__file__).resolve().parents[2] / "scripts" / "matrx-git-credential-env"


def _run_helper(stdin: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "MATRX_GITHUB_TOKEN"):
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
