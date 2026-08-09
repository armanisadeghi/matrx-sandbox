"""Regression tests for fail-closed release promotion contracts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_GUARD = REPO_ROOT / "scripts" / "lib" / "release-guard.sh"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HOSTED_DEPLOY = REPO_ROOT / "scripts" / "deploy-hosted.sh"
AIDREAM_BUILDER = REPO_ROOT / "sandbox-image" / "build-aidream.sh"
LEGACY_EC2_SOURCE_SHAS = (
    "30ed118b431b72e8f73f1b199fd9398d78361ed5",
    "f229d4b9347a66b3e8e8d8235f122d31dc336436",
)


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


def _run_approved_guard(
    checkout: Path, deployed: str, target: str
) -> subprocess.CompletedProcess[str]:
    script = """
set -u
fail() { echo "$*" >&2; exit 1; }
source "$1"
release_guard_fetch_approved_release "$2" "$4"
release_guard_assert_descendant "$2" "$3" "$4" "deployed revision"
"""
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "guard-test",
            str(RELEASE_GUARD),
            str(checkout),
            deployed,
            target,
        ],
        text=True,
        capture_output=True,
    )


def _run_legacy_bootstrap(
    checkout: Path,
    live_dir: Path,
    legacy_shas: tuple[str, ...] = LEGACY_EC2_SOURCE_SHAS,
) -> subprocess.CompletedProcess[str]:
    script = """
set -u
fail() { echo "$*" >&2; exit 1; }
source "$1"
release_guard_bootstrap_legacy_source "$2" "$3" "$4" orchestrator
"""
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "legacy-test",
            str(RELEASE_GUARD),
            str(checkout),
            str(live_dir),
            " ".join(legacy_shas),
        ],
        text=True,
        capture_output=True,
    )


def _extract_legacy_orchestrator(
    checkout: Path, destination: Path, legacy_sha: str = LEGACY_EC2_SOURCE_SHAS[0]
) -> None:
    destination.mkdir()
    archive = subprocess.Popen(
        ["git", "archive", legacy_sha, "orchestrator"],
        cwd=checkout,
        stdout=subprocess.PIPE,
    )
    assert archive.stdout is not None
    subprocess.run(
        ["tar", "-x", "--strip-components=1", "-C", str(destination)],
        stdin=archive.stdout,
        check=True,
    )
    archive.stdout.close()
    assert archive.wait() == 0


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


def test_approved_release_survives_unapproved_main_advance(tmp_path: Path):
    checkout, author, first, second = _release_history(tmp_path)
    approval_ref = f"refs/tags/deploy-approved/{first}"

    # A was current when approved. B then becomes main but remains unapproved;
    # rollout of A is still authorized by its immutable approval identity.
    _git(author, "push", "--force", "origin", f"{first}:refs/heads/main")
    _git(author, "push", "origin", f"{first}:{approval_ref}")
    _git(author, "push", "origin", f"{second}:refs/heads/main")

    approved_a = _run_approved_guard(checkout, first, first)
    unapproved_b = _run_approved_guard(checkout, first, second)
    downgrade_to_a = _run_approved_guard(checkout, second, first)

    assert approved_a.returncode == 0, approved_a.stderr
    assert unapproved_b.returncode != 0
    assert "cannot resolve immutable approval ref" in unapproved_b.stderr
    assert downgrade_to_a.returncode != 0
    assert "does not descend" in downgrade_to_a.stderr


def test_workflow_approves_only_current_main_then_revalidates_immutable_ref():
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    approval = workflow[
        workflow.index("- name: Promote tested commit to hosted approval ref") :
        workflow.index("\n  deploy:")
    ]
    revalidation = workflow[
        workflow.index("- name: Revalidate immutable approved release") :
        workflow.index("- name: Configure AWS credentials")
    ]

    assert 'git rev-parse refs/remotes/origin/main)" = "$GITHUB_SHA"' in approval
    assert 'refs/tags/deploy-approved/$GITHUB_SHA' in approval
    assert "git push --atomic" in approval
    assert "--force-with-lease=refs/heads/main:$GITHUB_SHA origin" in approval
    assert '"${GITHUB_SHA}:refs/heads/main"' in approval
    assert '"${GITHUB_SHA}:${APPROVAL_REF}"' in approval
    assert 'refs/tags/deploy-approved/$GITHUB_SHA' in revalidation
    assert "origin/main" not in revalidation


def test_ec2_legacy_source_bootstraps_each_exact_known_layout(tmp_path: Path):
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", str(REPO_ROOT), str(checkout))
    for index, legacy_sha in enumerate(LEGACY_EC2_SOURCE_SHAS):
        live_dir = tmp_path / f"live-{index}"
        _extract_legacy_orchestrator(checkout, live_dir, legacy_sha)
        (live_dir / ".env").write_text("MATRX_API_KEY=test\n", encoding="utf-8")
        (live_dir / ".venv").mkdir()
        cache = live_dir / "orchestrator" / "__pycache__"
        cache.mkdir()
        (cache / "main.cpython-311.pyc").write_bytes(b"runtime cache")

        result = _run_legacy_bootstrap(checkout, live_dir)

        assert result.returncode == 0, result.stderr
        assert (live_dir / ".source-sha").read_text(encoding="utf-8") == (
            f"{legacy_sha}\n"
        )


def test_ec2_legacy_source_rejects_missing_tampered_and_ambiguous_layouts(
    tmp_path: Path,
):
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", str(REPO_ROOT), str(checkout))

    missing = tmp_path / "missing"
    missing.mkdir()
    tampered = tmp_path / "tampered"
    _extract_legacy_orchestrator(checkout, tampered)
    (tampered / "orchestrator" / "main.py").write_text("# tampered\n", encoding="utf-8")
    ambiguous = tmp_path / "ambiguous"
    _extract_legacy_orchestrator(checkout, ambiguous)
    (ambiguous / "unexpected.py").write_text("pass\n", encoding="utf-8")
    symlinked_marker = tmp_path / "symlinked-marker"
    _extract_legacy_orchestrator(checkout, symlinked_marker)
    (symlinked_marker / ".source-sha").symlink_to(tmp_path / "absent-marker")

    for live_dir in (missing, tampered, ambiguous, symlinked_marker):
        result = _run_legacy_bootstrap(checkout, live_dir)
        assert result.returncode != 0
        assert not (live_dir / ".source-sha").is_file()


def test_workflow_uses_sha_as_single_ecr_release_pointer():
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    # Core and orchestrator use separate ECR_REPO values but the same immutable
    # tag expression, so this exact push must occur twice.
    assert workflow.count('docker push $ECR_REPO:${{ github.sha }}') == 2
    assert 'docker push $ECR_REPO:slim-${{ github.sha }}' in workflow
    assert not re.search(r"docker push .*:(?:latest|slim)[\"']?\s*$", workflow, re.MULTILINE)
    assert "Promote verified ECR aliases" not in workflow


def test_ci_test_checkout_includes_legacy_release_history():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    test_job = workflow[workflow.index("  test:") : workflow.index("\n  build-sandbox:")]

    assert "fetch-depth: 0" in test_job


def test_hosted_deploy_self_heals_images_at_fleet_freshness_limit():
    script = HOSTED_DEPLOY.read_text(encoding="utf-8")

    assert 'MAX_IMAGE_AGE_SECONDS="${MAX_IMAGE_AGE_SECONDS:-1209600}"' in script
    assert '|| image_too_old "$1"' in script
    for image in ("core", "slim", "aidream", "local"):
        assert f"&& ! image_too_old matrx-sandbox:{image}" in script


def test_hosted_deploy_pins_aidream_source_before_long_build():
    deploy = HOSTED_DEPLOY.read_text(encoding="utf-8")
    builder = AIDREAM_BUILDER.read_text(encoding="utf-8")

    assert '--source-sha "$AIDREAM_SOURCE_SHA"' in deploy
    assert '--source-sha) SOURCE_SHA="$2"' in builder
    assert 'archive --format=tar "$SOURCE_REF"' in builder


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


def test_deploy_scripts_revalidate_immutable_approval_not_moving_main():
    for relative_path in ("scripts/deploy-ec2.sh", "scripts/deploy-hosted.sh"):
        script = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "release_guard_fetch_approved_release" in script
        assert "release_guard_fetch_current_main" not in script


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
