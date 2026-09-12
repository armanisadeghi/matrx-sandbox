from __future__ import annotations

import os
import pwd
import shlex
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "sandbox-image/scripts/prepare-agent-home.sh"
CORE = ROOT / "sandbox-image/scripts/entrypoint.sh"
LAYOUT = ROOT / "sandbox-image/scripts/ensure-layout.sh"


def test_migration_retained_home_preserves_metadata_and_terminates_existing_key_line(tmp_path: Path):
    """A promoted home must retain content while a missing newline cannot concatenate SSH keys."""
    home, admin = tmp_path / "home", tmp_path / "admin_keys"
    ssh = home / ".ssh"; ssh.mkdir(parents=True)
    custom = "ssh-ed25519 CUSTOM user@custom"
    managed = "ssh-ed25519 MANAGED admin@matrx"
    keys = ssh / "authorized_keys"; keys.write_text(custom)
    payload = home / "project"; payload.write_text("keep")
    os.chmod(payload, 0o640); before = payload.stat()
    admin.write_text(managed + "\n")
    # A non-existent owner proves migration mode avoided the normal new-home chown.
    subprocess.run(
        ["bash", str(HELPER)],
        check=True,
        env={**os.environ, "HOT_PATH": str(home), "ADMIN_KEYS_PATH": str(admin), "AGENT_USER": "must-not-be-chowned", "SANDBOX_MIGRATION": "1"},
    )
    assert payload.stat().st_mode == before.st_mode
    assert payload.stat().st_uid == before.st_uid
    assert keys.read_text() == custom + "\n" + managed + "\n"


def test_home_helper_refuses_symlinked_authorized_keys(tmp_path: Path):
    """A root entrypoint must not follow a retained-home key symlink."""
    home, admin, target = tmp_path / "home", tmp_path / "admin_keys", tmp_path / "outside"
    (home / ".ssh").mkdir(parents=True)
    target.write_text("outside must remain unchanged\n")
    (home / ".ssh" / "authorized_keys").symlink_to(target)
    admin.write_text("ssh-ed25519 MANAGED admin@matrx\n")
    result = subprocess.run(
        ["bash", str(HELPER)],
        text=True,
        capture_output=True,
        env={**os.environ, "HOT_PATH": str(home), "ADMIN_KEYS_PATH": str(admin), "SANDBOX_MIGRATION": "1"},
    )
    assert result.returncode != 0
    assert "refusing symlink authorized_keys" in result.stderr
    assert target.read_text() == "outside must remain unchanged\n"


def _write_and_read_as_agent(path: Path) -> str:
    command = f"printf agent-can-write > {shlex.quote(str(path))}; cat {shlex.quote(str(path))}"
    if os.geteuid() != 0:
        return subprocess.run(["sh", "-c", command], check=True, text=True, capture_output=True).stdout
    try:
        pwd.getpwnam("agent")
    except KeyError:
        pytest.skip("root test environment has no real agent account")
    return subprocess.run(["su", "-s", "/bin/sh", "agent", "-c", command], check=True, text=True, capture_output=True).stdout


def test_fresh_layout_creates_agent_writable_canonical_directories(tmp_path: Path):
    """New .matrx descendants must be writable by the real sandbox agent, not root-owned 0755."""
    home = tmp_path / "home"
    agent_user = "agent" if os.geteuid() == 0 else pwd.getpwuid(os.geteuid()).pw_name
    subprocess.run(["bash", str(LAYOUT)], check=True, env={**os.environ, "AGENT_HOME": str(home), "AGENT_USER": agent_user})
    for relative in (".matrx/plans", ".matrx/skills", ".matrx/runtime/tool-calls", "cloud-files", "repos", "projects", "scratch"):
        directory = home / relative
        assert directory.is_dir()
        assert _write_and_read_as_agent(directory / "agent-write.txt") == "agent-can-write"


def test_layout_leaves_existing_user_directory_metadata_untouched(tmp_path: Path):
    """Retained descendants are user data: layout may add missing paths but cannot repair their mode."""
    home = tmp_path / "home"
    plans = home / ".matrx" / "plans"
    plans.mkdir(parents=True)
    os.chmod(plans, 0o750)
    before = plans.stat()
    subprocess.run(["bash", str(LAYOUT)], check=True, env={**os.environ, "AGENT_HOME": str(home)})
    after = plans.stat()
    assert after.st_mode == before.st_mode
    assert after.st_uid == before.st_uid


def test_core_migration_boot_branches_before_hot_download(tmp_path: Path):
    """Migration mode must select the no-download branch before any initial sync call."""
    script = CORE.read_text()
    start = script.index('if [ "${SANDBOX_MIGRATION:-}" = "1" ]; then')
    end = script.index('# ─── Step 2:', start)
    block = script[start:end]
    # Execute the production block with a real command seam: an accidental hot
    # download writes a receipt and causes the migration assertion to fail.
    marker = tmp_path / "hot-sync-called"
    hot_sync = tmp_path / "hot-sync.sh"
    hot_sync.write_text(f"#!/usr/bin/env bash\nprintf called > {shlex.quote(str(marker))}\n")
    hot_sync.chmod(0o755)
    step_one = script[start:end].replace("/opt/sandbox/scripts/hot-sync.sh", str(hot_sync))

    migration = subprocess.run(
        ["bash", "-c", step_one],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "SANDBOX_MIGRATION": "1"},
    )
    assert "skipping hot storage download" in migration.stdout
    assert not marker.exists()

    normal = subprocess.run(
        ["bash", "-c", step_one],
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "SANDBOX_MIGRATION": "0"},
    )
    assert "Hot storage sync complete" in normal.stdout
    assert marker.read_text() == "called"
