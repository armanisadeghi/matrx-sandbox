"""The Python half of "the shell sees what the daemon sees".

sshd hands the container environment to nothing it starts, so `mtx` invoked
over SSH (``ssh box 'mtx files ls'`` reads neither /etc/profile nor ~/.bashrc)
saw no identity and announced "AI Dream not configured for this sandbox" — on a
box that was fully wired. The entrypoints publish the identity to
/etc/matrx/bridge-env.sh; these tests hold the contract this module reads it by.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from matrx_agent import bridge_headers
from matrx_agent.bridge_headers import load_published_identity, missing_bridge_env


def _write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "bridge-env.sh"
    target.write_text(body, encoding="utf-8")
    return target


def test_published_identity_fills_what_the_shell_never_received(tmp_path: Path) -> None:
    source = _write(
        tmp_path,
        'export USER_ID="user-1"\n'
        "export ORGANIZATION_ID='org 2'\n"
        'export MATRX_AIDREAM_URL="https://server.example.test"\n'
        'export MATRX_AIDREAM_SERVICE_TOKEN="tok"\n',
    )
    env: dict[str, str] = {}

    filled = load_published_identity(source, env)

    assert sorted(filled) == [
        "MATRX_AIDREAM_SERVICE_TOKEN", "MATRX_AIDREAM_URL",
        "ORGANIZATION_ID", "USER_ID",
    ]
    assert env["ORGANIZATION_ID"] == "org 2"
    assert missing_bridge_env(env) == []


def test_the_process_environment_always_wins(tmp_path: Path) -> None:
    """A stale file must never switch the tenant a sandbox writes into."""
    source = _write(tmp_path, 'export ORGANIZATION_ID="stale-org"\nexport USER_ID="stale"\n')
    env = {"ORGANIZATION_ID": "live-org"}

    load_published_identity(source, env)

    assert env["ORGANIZATION_ID"] == "live-org"
    assert env["USER_ID"] == "stale"  # only the ABSENT one was filled


def test_an_absent_file_is_not_a_failure(tmp_path: Path) -> None:
    """An unwired image has no such file, and that is not a defect."""
    env: dict[str, str] = {}

    assert load_published_identity(tmp_path / "nope.sh", env) == []
    assert env == {}


def test_only_the_identity_variables_are_taken(tmp_path: Path) -> None:
    """The file is an identity channel, not a general env injector."""
    source = _write(tmp_path, 'export PATH="/evil"\nexport USER_ID="user-1"\n')
    env: dict[str, str] = {}

    load_published_identity(source, env)

    assert env == {"USER_ID": "user-1"}


def test_missing_bridge_env_consults_the_published_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real entry point: what every client calls before refusing."""
    source = _write(
        tmp_path,
        'export USER_ID="user-1"\n'
        'export ORGANIZATION_ID="org-2"\n'
        'export MATRX_AIDREAM_URL="https://server.example.test"\n'
        'export MATRX_AIDREAM_SERVICE_TOKEN="tok"\n',
    )
    for name in bridge_headers.REQUIRED_BRIDGE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bridge_headers, "PUBLISHED_IDENTITY_FILE", source)

    assert missing_bridge_env() == []

    from matrx_agent.cloud_sync.client import BridgeConfig

    cfg = BridgeConfig.from_env()
    assert cfg is not None and cfg.organization_id == "org-2"


def test_a_partial_published_identity_still_refuses_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half an identity is a provisioning defect, not a working sandbox."""
    source = _write(tmp_path, 'export USER_ID="user-1"\n')
    for name in bridge_headers.REQUIRED_BRIDGE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bridge_headers, "PUBLISHED_IDENTITY_FILE", source)

    assert "ORGANIZATION_ID" in missing_bridge_env()


def test_the_publisher_writes_the_same_variables_this_module_reads() -> None:
    """The two halves of the channel are held together at their names."""
    publisher = (
        Path(__file__).resolve().parents[2] / "scripts" / "write-bridge-env.sh"
    ).read_text(encoding="utf-8")

    for name in bridge_headers.REQUIRED_BRIDGE_ENV:
        assert name in publisher, f"{name} is never published by write-bridge-env.sh"
    # And it must not put the token anywhere the user's home volume survives.
    assert "/home/agent" not in publisher.split("Two rules this file obeys")[-1].split("set -euo")[-1]
