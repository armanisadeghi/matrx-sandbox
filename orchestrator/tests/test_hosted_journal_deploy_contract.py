"""Exercise deploy's filesystem boundary; Docker mount proof is a live gate."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path, *, docker_fails=False):
    source = (ROOT / "scripts/deploy-hosted.sh").read_text()
    function = "prepare_hosted_journal() {" + source.split("prepare_hosted_journal() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    # This fake records invocation only, not actual mount/UID acceptance.
    script = r'''
set -uo pipefail
fail() { printf '%s\n' "$1" >&2; exit 1; }
docker() {
  printf '%s\n' "$*" >> "$CASE_DIR/docker-calls"
  if [ "$DOCKER_FAILS" = 1 ]; then return 1; fi
  if [ "$1" = compose ]; then
    printf '{"services":{"orchestrator":{"volumes":[{"type":"bind","source":"%s/hosted-migrations","target":"/var/lib/matrx-sandbox/hosted-migrations"}]}}}' "$ORCH_COMPOSE_DIR"
  fi
}
'''
    env = dict(os.environ, REPO_DIR=str(ROOT), ORCH_COMPOSE_DIR=str(tmp_path),
               CASE_DIR=str(tmp_path), DOCKER_FAILS="1" if docker_fails else "0")
    return subprocess.run(["bash", "-c", script + function + '\nprepare_hosted_journal "sha256:test"'],
                          env=env, text=True, capture_output=True)


def test_preflight_installs_additive_overlay_and_verifies_actual_mount(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "hosted-migrations").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "docker-compose.override.yml").read_bytes() == (ROOT / "infra/hosted/docker-compose.override.yml").read_bytes()
    calls = (tmp_path / "docker-calls").read_text()
    assert "--network none --entrypoint python --mount type=bind,src=" in calls
    assert "HostedMigrationJournal().ensure_ready()" in calls
    assert _run(tmp_path).returncode == 0


def test_unknown_operator_override_is_never_overwritten(tmp_path):
    override = tmp_path / "docker-compose.override.yml"
    override.write_text("services: {operator-owned: {}}\n")
    before = override.read_bytes()
    result = _run(tmp_path)
    assert result.returncode != 0 and "differs from canonical" in result.stderr
    assert override.read_bytes() == before
    assert not (tmp_path / "docker-calls").exists()


@pytest.mark.parametrize("name", ["hosted-migrations", "docker-compose.override.yml"])
def test_symlink_substitution_refused_before_docker(tmp_path, name):
    (tmp_path / name).symlink_to(tmp_path / "unrelated")
    result = _run(tmp_path)
    assert result.returncode != 0 and "symbolic links" in result.stderr
    assert not (tmp_path / "docker-calls").exists()


def test_candidate_uid_or_mount_failure_prevents_deploy(tmp_path):
    result = _run(tmp_path, docker_fails=True)
    assert result.returncode != 0 and "cannot safely use durable" in result.stderr
    assert "compose" not in (tmp_path / "docker-calls").read_text()
    assert not (tmp_path / "docker-compose.override.yml").exists()
