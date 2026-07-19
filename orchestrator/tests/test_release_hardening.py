"""Regression tests for fail-closed release promotion contracts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_GUARD = REPO_ROOT / "scripts" / "lib" / "release-guard.sh"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _release_history(tmp_path: Path) -> tuple[Path, Path, str, str]:
    origin = tmp_path / "origin.git"
    author = tmp_path / "author"
    checkout = tmp_path / "checkout"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "-b", "main", str(author))
    _git(author, "config", "user.email", "release-test@example.com")
    _git(author, "config", "user.name", "Release Test")
    (author / "release.txt").write_text("one\n", encoding="utf-8")
    _git(author, "add", "release.txt")
    _git(author, "commit", "-m", "first")
    first = _git(author, "rev-parse", "HEAD")
    (author / "release.txt").write_text("two\n", encoding="utf-8")
    _git(author, "commit", "-am", "second")
    second = _git(author, "rev-parse", "HEAD")
    _git(author, "remote", "add", "origin", str(origin))
    _git(author, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", "--branch", "main", str(origin), str(checkout))
    return checkout, author, first, second


def _run_guard(checkout: Path, deployed: str, target: str) -> subprocess.CompletedProcess[str]:
    script = """
set -u
fail() { echo "$*" >&2; exit 1; }
source "$1"
release_guard_fetch_current_main "$2" "$4"
release_guard_assert_descendant "$2" "$3" "$4" "deployed revision"
"""
    return subprocess.run(
        ["bash", "-c", script, "guard-test", str(RELEASE_GUARD), str(checkout), deployed, target],
        text=True,
        capture_output=True,
    )


def test_release_guard_accepts_only_current_forward_release(tmp_path: Path):
    checkout, author, first, second = _release_history(tmp_path)

    forward = _run_guard(checkout, first, second)
    historical_rerun = _run_guard(checkout, first, first)
    # Failure injection: even if the remote main ref is forcibly moved back,
    # the independently recorded deployed revision still blocks the downgrade.
    _git(author, "push", "--force", "origin", f"{first}:refs/heads/main")
    downgrade = _run_guard(checkout, second, first)

    assert forward.returncode == 0, forward.stderr
    assert historical_rerun.returncode != 0
    assert "not current origin/main" in historical_rerun.stderr
    assert downgrade.returncode != 0
    assert "does not descend" in downgrade.stderr


def test_workflow_uses_sha_as_single_ecr_release_pointer():
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    # Core and orchestrator use separate ECR_REPO values but the same immutable
    # tag expression, so this exact push must occur twice.
    assert workflow.count('docker push $ECR_REPO:${{ github.sha }}') == 2
    assert 'docker push $ECR_REPO:slim-${{ github.sha }}' in workflow
    assert not re.search(r"docker push .*:(?:latest|slim)[\"']?\s*$", workflow, re.MULTILINE)
    assert "Promote verified ECR aliases" not in workflow


def test_workflows_pin_uv_version_on_the_uv_action_only():
    for name in ("ci.yml", "deploy.yml"):
        workflow = (DEPLOY_WORKFLOW.parent / name).read_text(encoding="utf-8")
        setup_python = re.findall(
            r"uses: actions/setup-python@.*?\n\s+with:\n(?P<inputs>(?:\s{10,}.*\n)+)",
            workflow,
        )
        setup_uv = re.findall(
            r"uses: astral-sh/setup-uv@.*?\n\s+with:\n(?P<inputs>(?:\s{10,}.*\n)+)",
            workflow,
        )
        assert setup_python and all(
            not re.search(r"^\s*version:", block, re.MULTILINE)
            for block in setup_python
        )
        assert setup_uv and all(
            re.search(r'^\s*version: "0.10.8"', block, re.MULTILINE)
            for block in setup_uv
        )


def test_deploy_scripts_guard_ancestry_before_migrations():
    migrations = {
        "scripts/deploy-ec2.sh": '"$CANDIDATE_DIR/.venv/bin/python" -m orchestrator.migrate_runner',
        "scripts/deploy-hosted.sh": 'run_db_migrations "$ORCH_CANDIDATE"',
    }
    for relative_path, migration_call in migrations.items():
        script = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        guard_at = script.index("release_guard_assert_descendant")
        migration_at = script.index(migration_call, guard_at)
        validations = [
            match.start()
            for match in re.finditer(
                r"^\s*validate_release_authority\s*$",
                script,
                re.MULTILINE,
            )
        ]
        assert guard_at < migration_at
        assert any(guard_at < position < migration_at for position in validations)
        assert any(position > migration_at for position in validations)


def test_hosted_noop_requires_every_live_release_alias():
    script = (REPO_ROOT / "scripts" / "deploy-hosted.sh").read_text(encoding="utf-8")
    start = script.index("hosted_release_complete()")
    complete = script[start : script.index('if [ "${FORCE:-0}"', start)]
    for image in (
        '"$ORCH_IMAGE"',
        "matrx-sandbox:core",
        "matrx-sandbox:slim",
        "matrx-sandbox:aidream",
        "matrx-sandbox:local",
    ):
        assert image in complete
    assert "if ! hosted_release_complete" in script
