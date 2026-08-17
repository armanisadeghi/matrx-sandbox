"""Regression tests for fail-closed release promotion contracts."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_GUARD = REPO_ROOT / "scripts" / "lib" / "release-guard.sh"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HOSTED_DEPLOY = REPO_ROOT / "scripts" / "deploy-hosted.sh"
AIDREAM_BUILDER = REPO_ROOT / "sandbox-image" / "build-aidream.sh"
AIDREAM_HELPER = REPO_ROOT / "sandbox-image" / "scripts" / "aidream-helpers.sh"
AIDREAM_ENTRYPOINT = REPO_ROOT / "sandbox-image" / "scripts" / "entrypoint-aidream.sh"
AIDREAM_DOCKERFILE = REPO_ROOT / "sandbox-image" / "Dockerfile.aidream"
SANDBOX_ROUTES = REPO_ROOT / "orchestrator" / "orchestrator" / "routes" / "sandboxes.py"
CORE_DOCKERFILE = REPO_ROOT / "sandbox-image" / "Dockerfile"
SLIM_DOCKERFILE = REPO_ROOT / "sandbox-image" / "Dockerfile.slim"
HOSTED_DEPLOY_SERVICE = (
    REPO_ROOT / "scripts" / "systemd" / "matrx-hosted-deploy.service"
)
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


def test_hosted_fast_path_allows_one_serialized_recovery_ahead_of_it():
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    hosted_job = workflow[workflow.index("  deploy-hosted:") :]

    assert "command_timeout: 120m" in hosted_job


def test_authoritative_poller_allows_a_queued_cold_build():
    unit = HOSTED_DEPLOY_SERVICE.read_text(encoding="utf-8")
    deploy = HOSTED_DEPLOY.read_text(encoding="utf-8")

    assert "TimeoutStartSec=3h" in unit
    assert "matrx-hosted-deploy.service" in deploy
    assert "systemctl daemon-reload" in deploy


def test_image_version_stamp_does_not_invalidate_dependency_cache():
    core = CORE_DOCKERFILE.read_text(encoding="utf-8")
    slim = SLIM_DOCKERFILE.read_text(encoding="utf-8")

    assert core.index("ARG MATRX_IMAGE_VERSION") > core.index("COPY sdk/")
    assert slim.index("ARG MATRX_IMAGE_VERSION") > slim.index("COPY sdk/")


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


def test_aidream_builder_and_autostart_require_exact_full_source_sha():
    builder = AIDREAM_BUILDER.read_text(encoding="utf-8")
    entrypoint = AIDREAM_ENTRYPOINT.read_text(encoding="utf-8")
    routes = SANDBOX_ROUTES.read_text(encoding="utf-8")

    assert '--build-arg AIDREAM_GIT_SHA="$AIDREAM_FULL_SHA"' in builder
    assert 'git fetch -q --depth=1 "$AIDREAM_SRC" "$AIDREAM_FULL_SHA"' in builder
    assert 'test "$(git rev-parse HEAD)" = "$AIDREAM_FULL_SHA"' in builder
    assert 'IMAGE_AIDREAM_SHA=$(docker image inspect' in builder
    assert "--require-image-source" in entrypoint
    assert '"verify-release"' in routes
    assert 'environment={"AIDREAM_WORK_DIR": "/opt/aidream-template"}' in routes
    assert '"aidream_source_exact": aidream_source' in routes
    assert 'aidream_source.get("ok")' in routes


def test_aidream_autostart_uses_immutable_template_without_resetting_user_work():
    entrypoint = AIDREAM_ENTRYPOINT.read_text(encoding="utf-8")
    dockerfile = AIDREAM_DOCKERFILE.read_text(encoding="utf-8")
    autostart = entrypoint[entrypoint.index('log "auto-starting aidream') :]

    assert 'AIDREAM_WORK_DIR="$TEMPLATE_DIR"' in autostart
    assert "/bin/bash --noprofile --norc -p" in autostart
    assert "/opt/sandbox/scripts/aidream-helpers.sh serve" in autostart
    assert "bash -lc" not in autostart
    assert "bash -c" not in autostart
    assert "-u BASH_ENV -u ENV" in autostart
    assert "-u PYTHONHOME -u PYTHONPATH -u PYTHONSTARTUP" in autostart
    assert "-u LD_PRELOAD -u LD_LIBRARY_PATH" in autostart
    assert "-u GIT_CONFIG_SYSTEM -u GIT_CONFIG_NOSYSTEM" in autostart
    assert "-u VIRTUAL_ENV -u UV_PROJECT_ENVIRONMENT -u UV_PYTHON" in autostart
    assert 'PATH="$TEMPLATE_DIR/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' in autostart
    assert 'AIDREAM_TEMPLATE_DIR="$TEMPLATE_DIR"' in autostart
    assert "AIDREAM_IMAGE_SHA_FILE=/etc/aidream-image-sha" in autostart
    assert "HOME=/run/aidream-managed-home" in autostart
    assert "GIT_CONFIG_GLOBAL=/dev/null" in autostart
    assert "PYTHONNOUSERSITE=1" in autostart
    assert "PYTHONDONTWRITEBYTECODE=1" in autostart
    assert "template_mount_is_read_only" in entrypoint
    assert '[[ ",$options," == *,ro,* ]]' in entrypoint
    assert "refusing managed aidream autostart" in entrypoint
    assert 'export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' in entrypoint
    assert '/usr/bin/findmnt -n -o OPTIONS -T "$TEMPLATE_DIR"' in entrypoint
    assert "/usr/bin/sudo -u agent -E -H /usr/bin/env" in entrypoint
    assert "/bin/sleep 8" in entrypoint
    assert '[ "${MATRX_TIER:-}" = "hosted" ]' in entrypoint
    assert "Claude managed runtime is hosted-only" in entrypoint
    assert "--require-image-source" in autostart
    assert 'rm -rf "$WORK_DIR"' not in entrypoint
    assert 'found existing $WORK_DIR — preserving user state' in entrypoint
    assert "COPY --chown=root:root ./aidream-src/" in dockerfile
    assert 'chown -R root:root "${AIDREAM_TEMPLATE_DIR}"' in dockerfile
    assert 'chmod -R a-w "${AIDREAM_TEMPLATE_DIR}"' in dockerfile
    assert 'git config --system --add safe.directory "${AIDREAM_TEMPLATE_DIR}"' in dockerfile
    assert "mkdir -p /var/log/aidream" in dockerfile
    assert "chown agent:agent /var/log/aidream" in dockerfile


def test_aidream_build_proves_agent_cannot_mutate_certified_runtime():
    builder = AIDREAM_BUILDER.read_text(encoding="utf-8")

    assert "docker run --rm --read-only" in builder
    assert "findmnt -n -o OPTIONS -T /opt/aidream-template" in builder
    assert "test ! -w /opt/aidream-template" in builder
    assert "test ! -w /opt/aidream-template/pyproject.toml" in builder
    assert "test ! -w /opt/aidream-template/.venv" in builder
    assert "sudo -u agent sudo touch /opt/aidream-template/.sudo-write-probe" in builder
    assert "sudo -u agent sudo touch /opt/aidream-template/.venv/.sudo-write-probe" in builder
    assert "sudo /bin/mount -o remount,rw /" in builder
    assert "sudo /bin/mount --bind /home/agent/aidream /opt/aidream-template" in builder
    assert "AIDREAM_WORK_DIR=/opt/aidream-template" in builder
    assert "aidream-helpers.sh verify-release" in builder
    assert "touch /var/log/aidream/.agent-log-probe" in builder
    assert "sitecustomize.py" in builder
    assert "fsmonitor = !touch /tmp/gitconfig-ran" in builder
    assert "test ! -e /tmp/sitecustomize-ran" in builder
    assert "test ! -e /tmp/gitconfig-ran" in builder
    assert "for shim in findmnt sudo env sleep bash" in builder
    assert 'compgen -G "/tmp/shim-*-ran"' in builder


def test_aidream_autostart_cannot_source_malicious_agent_profiles():
    entrypoint = AIDREAM_ENTRYPOINT.read_text(encoding="utf-8")
    autostart = entrypoint[entrypoint.index('log "auto-starting aidream') :]

    # A malicious ~/.bash_profile, ~/.profile, ~/.bashrc, or BASH_ENV cannot
    # execute because managed autostart invokes the root-owned command
    # directly rather than starting any shell, and strips shell hooks before
    # dispatch. The explicit PATH also cannot be replaced by a user profile.
    assert "bash -l" not in autostart
    assert "bash -c" not in autostart
    assert ".bash_profile" not in autostart
    assert ".bashrc" not in autostart
    assert "/bin/bash --noprofile --norc -p" in autostart
    assert "/opt/sandbox/scripts/aidream-helpers.sh serve" in autostart
    assert "-u BASH_ENV -u ENV" in autostart
    assert 'PATH="$TEMPLATE_DIR/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' in autostart


def test_managed_aidream_uses_fixed_venv_without_uv_mutation():
    helper = AIDREAM_HELPER.read_text(encoding="utf-8")

    assert 'if [ "$require_image_source" -eq 1 ]' in helper
    assert 'nohup "$WORK_DIR/.venv/bin/python" -I run.py' in helper
    assert "PYTHONDONTWRITEBYTECODE=1" in helper


def test_exact_release_verifier_rejects_untracked_tampering():
    helper = AIDREAM_HELPER.read_text(encoding="utf-8")

    assert "status --porcelain --untracked-files=all" in helper


def test_every_aidream_container_path_uses_shared_isolation_and_readiness():
    manager = (REPO_ROOT / "orchestrator/orchestrator/sandbox_manager.py").read_text()
    migrate = (REPO_ROOT / "orchestrator/orchestrator/migrate.py").read_text()
    pool = (REPO_ROOT / "orchestrator/orchestrator/pool.py").read_text()

    assert "**container_runtime_isolation(template, location.tier)" in manager
    assert migrate.count("run_kwargs.update(container_runtime_isolation(template, tier))") == 2
    assert '_wait_container_ready(new, verify_timeout, template)' in migrate
    assert "http://127.0.0.1:8001/api/health/ready" in migrate
    assert "aidream-helpers.sh verify-release" in migrate
    assert "if not warm_pool_supports_template(template)" in pool


def test_aidream_release_source_verifier_rejects_dirty_and_wrong_sha(tmp_path: Path):
    workdir = tmp_path / "aidream"
    workdir.mkdir()
    _git(workdir, "init", "-b", "main")
    _git(workdir, "config", "user.email", "release-test@example.com")
    _git(workdir, "config", "user.name", "Release Test")
    tracked = workdir / "release.txt"
    tracked.write_text("exact\n", encoding="utf-8")
    _git(workdir, "add", "release.txt")
    _git(workdir, "commit", "-m", "exact source")
    exact_sha = _git(workdir, "rev-parse", "HEAD")
    sha_file = tmp_path / "aidream-image-sha"
    sha_file.write_text(f"{exact_sha}\n", encoding="utf-8")
    env = {
        **os.environ,
        "AIDREAM_WORK_DIR": str(workdir),
        "AIDREAM_IMAGE_SHA_FILE": str(sha_file),
    }

    exact = subprocess.run(
        [str(AIDREAM_HELPER), "verify-release"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert exact.returncode == 0, exact.stderr
    assert f"source_state=exact expected={exact_sha} actual={exact_sha}" in exact.stdout

    tracked.write_text("modified\n", encoding="utf-8")
    dirty = subprocess.run(
        [str(AIDREAM_HELPER), "verify-release"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert dirty.returncode != 0
    assert "source_state=modified" in dirty.stderr

    _git(workdir, "restore", "release.txt")
    untracked = workdir / "sitecustomize.py"
    untracked.write_text("raise RuntimeError('tampered')\n", encoding="utf-8")
    untracked_result = subprocess.run(
        [str(AIDREAM_HELPER), "verify-release"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert untracked_result.returncode != 0
    assert "source_state=modified" in untracked_result.stderr
    assert "sitecustomize.py" in untracked_result.stderr

    untracked.unlink()
    sha_file.write_text(f"{'0' * 40}\n", encoding="utf-8")
    mismatch = subprocess.run(
        [str(AIDREAM_HELPER), "verify-release"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert mismatch.returncode != 0
    assert "source_state=sha-mismatch" in mismatch.stderr


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


def test_ec2_release_requires_the_private_aidream_replica_before_and_after_swap():
    script = (REPO_ROOT / "scripts" / "deploy-ec2.sh").read_text(encoding="utf-8")
    expected = 'EXPECTED_AIDREAM_URL="http://172.31.83.75:8000"'
    preflight = '[ "$AIDREAM_URL" = "$EXPECTED_AIDREAM_URL" ]'
    promotion = script.index('log "promoting candidates"')
    live_gate = '[ "$LIVE_AIDREAM_URL" != "$EXPECTED_AIDREAM_URL" ]'

    assert expected in script
    assert script.index(preflight) < promotion
    assert script.index(live_gate) > promotion
    assert '"$LIVE_AIDREAM_URL/health/version"' in script
    assert "never the public app_server" in script
    live_gate_at = script.index(live_gate)
    post_swap = script[live_gate_at : script.index("trap - ERR INT TERM", live_gate_at)]
    assert post_swap.count("rollback") == 2
    assert "|| fail" not in post_swap


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
