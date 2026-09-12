"""Migration hold boot regressions: pre-CAS startup cannot touch /home/agent."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "sandbox-image/scripts/prepare-agent-home.sh"
LAYOUT = ROOT / "sandbox-image/scripts/ensure-layout.sh"


def _script_with_test_marker(script: Path, marker: Path, tmp_path: Path) -> Path:
    """Exercise production shell behavior without making its root marker configurable."""
    copy = tmp_path / script.name
    copy.write_text(script.read_text().replace("/tmp/.matrx-migration-committed", str(marker)))
    copy.chmod(0o755)
    return copy


def test_hold_refuses_home_preparation_or_layout_writes_before_marker(tmp_path: Path):
    """Break caught: a pre-CAS target creates home paths or changes retained metadata."""
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
    for script in (PREPARE, LAYOUT):
        result = subprocess.run(
            ["bash", str(_script_with_test_marker(script, marker, tmp_path))],
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0
        assert "migration hold active" in result.stderr
    assert retained.read_text() == "unchanged"
    assert retained.stat().st_mode == before.st_mode
    assert retained.stat().st_uid == before.st_uid
    assert not (home / ".ssh").exists()
    assert not (home / ".matrx").exists()


def test_commit_marker_reenables_normal_layout_creation(tmp_path: Path):
    """Break caught: a durable commit marker leaves the replacement permanently inert."""
    home, marker = tmp_path / "home", tmp_path / "commit-marker"
    marker.write_text("committed\n")
    result = subprocess.run(
        ["bash", str(_script_with_test_marker(LAYOUT, marker, tmp_path))],
        check=True,
        env={**os.environ, "AGENT_HOME": str(home), "MATRX_MIGRATION_HOLD": "1"},
        text=True,
        capture_output=True,
    )
    assert result.stderr == ""
    assert (home / ".matrx/runtime/tool-calls").is_dir()
