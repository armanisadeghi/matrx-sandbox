"""Migration hold boot regressions: pre-CAS startup cannot touch /home/agent."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "sandbox-image/scripts/prepare-agent-home.sh"
LAYOUT = ROOT / "sandbox-image/scripts/ensure-layout.sh"
GIT_CREDENTIALS = ROOT / "sandbox-image/scripts/configure-git-credentials.sh"
LOCAL_ENTRYPOINT = ROOT / "sandbox-local/scripts/entrypoint-local.sh"
CORE_ENTRYPOINT = ROOT / "sandbox-image/scripts/entrypoint.sh"
SLIM_ENTRYPOINT = ROOT / "sandbox-image/scripts/entrypoint-slim.sh"
AIDREAM_ENTRYPOINT = ROOT / "sandbox-image/scripts/entrypoint-aidream.sh"


def _script_with_test_marker(script: Path, marker: Path, tmp_path: Path) -> Path:
    """Exercise production shell behavior without making its root marker configurable."""
    copy = tmp_path / script.name
    copy.write_text(script.read_text().replace("/var/lib/matrx-migration/committed", str(marker)))
    copy.chmod(0o755)
    return copy


def test_hold_refuses_home_writes_before_and_after_commit_marker(tmp_path: Path):
    """Break caught: activation preserves retained metadata, not only pre-CAS hold."""
    home = tmp_path / "home"
    home.mkdir()
    retained = home / "keep"
    retained.write_text("unchanged")
    retained.chmod(0o640)
    before = retained.stat()
    marker = tmp_path / "commit-marker"
    env = {
        **os.environ,
        "HOT_PATH": str(home),
        "AGENT_HOME": str(home),
        "MATRX_MIGRATION_HOLD": "1",
        "ADMIN_KEYS_PATH": str(tmp_path / "not-read"),
    }
    for committed in (False, True):
        if committed:
            marker.write_text("committed\n")
        for script in (PREPARE, LAYOUT, GIT_CREDENTIALS):
            result = subprocess.run(
                ["bash", str(_script_with_test_marker(script, marker, tmp_path))],
                env=env,
                text=True,
                capture_output=True,
            )
            assert result.returncode == 0
            assert "preserves the mounted home" in result.stderr
        assert retained.read_text() == "unchanged"
        assert retained.stat().st_mode == before.st_mode
        assert retained.stat().st_uid == before.st_uid
        assert retained.stat().st_gid == before.st_gid
        assert not (home / ".ssh").exists()
        assert not (home / ".matrx").exists()


def test_every_image_entrypoint_quarantines_bootstrap_writes_from_activation():
    """Every migratable image must skip its normal home bootstrap after CAS too."""
    cases = {
        LOCAL_ENTRYPOINT: ('chown -R agent:agent "$HOT_PATH"', 'cat > /home/agent/.sandbox_env'),
        CORE_ENTRYPOINT: ('cat > /home/agent/.sandbox_env', '/opt/sandbox/scripts/ensure-layout.sh'),
        SLIM_ENTRYPOINT: ('cat > /home/agent/.sandbox_env', '/opt/sandbox/scripts/configure-git-credentials.sh'),
        AIDREAM_ENTRYPOINT: ('/usr/bin/sudo -E /bin/cp -a', 'retarget_editables'),
    }
    for path, mutations in cases.items():
        source = path.read_text()
        hold = source.index('if [ "${MATRX_MIGRATION_HOLD:-}" = "1" ]')
        wait = source.index('while [ ! -f "$MATRX_MIGRATION_COMMIT_MARKER" ]')
        activation = source.index('if [ "$MATRX_MIGRATION_ACTIVATION" = "0" ]; then')
        preserve = source.index("Migration activation — preserving", activation)
        for mutation in mutations:
            assert hold < wait < activation < source.index(mutation, activation) < preserve


def test_commit_marker_does_not_reenable_layout_mutation(tmp_path: Path):
    """The old bug resumed layout generation after CAS and changed retained ownership."""
    home, marker = tmp_path / "home", tmp_path / "commit-marker"
    home.mkdir()
    retained = home / "root-owned-or-custom-owned"
    retained.write_text("preserve bytes and metadata\n")
    retained.chmod(0o640)
    before = retained.stat()
    marker.write_text("committed\n")
    result = subprocess.run(
        ["bash", str(_script_with_test_marker(LAYOUT, marker, tmp_path))],
        check=True,
        env={**os.environ, "AGENT_HOME": str(home), "MATRX_MIGRATION_HOLD": "1"},
        text=True,
        capture_output=True,
    )
    assert "preserves the mounted home" in result.stderr
    assert retained.read_text() == "preserve bytes and metadata\n"
    assert retained.stat().st_mode == before.st_mode
    assert retained.stat().st_uid == before.st_uid
    assert retained.stat().st_gid == before.st_gid
    assert not (home / ".matrx").exists()
