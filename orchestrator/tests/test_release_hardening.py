"""Regression tests for fail-closed release promotion contracts."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_GUARD = REPO_ROOT / "scripts" / "lib" / "release-guard.sh"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
HOSTED_DEPLOY = REPO_ROOT / "scripts" / "deploy-hosted.sh"
AIDREAM_BUILDER = REPO_ROOT / "sandbox-image" / "build-aidream.sh"
AIDREAM_HELPER = REPO_ROOT / "sandbox-image" / "scripts" / "aidream-helpers.sh"
AIDREAM_ENTRYPOINT = REPO_ROOT / "sandbox-image" / "scripts" / "entrypoint-aidream.sh"
AIDREAM_MANAGED_BOOTSTRAP_TEST = (
    REPO_ROOT / "sandbox-image" / "scripts" / "test-aidream-managed-bootstrap.sh"
)
AIDREAM_MANAGED_BOOTSTRAP = (
    REPO_ROOT / "sandbox-image" / "scripts" / "aidream-managed-bootstrap.py"
)
AIDREAM_MANAGED_HOME_TEST = (
    REPO_ROOT / "sandbox-image" / "scripts" / "test-prepare-aidream-managed-home.sh"
)
AIDREAM_DOCKERFILE = REPO_ROOT / "sandbox-image" / "Dockerfile.aidream"
SANDBOX_ROUTES = REPO_ROOT / "orchestrator" / "orchestrator" / "routes" / "sandboxes.py"
CORE_DOCKERFILE = REPO_ROOT / "sandbox-image" / "Dockerfile"
SLIM_DOCKERFILE = REPO_ROOT / "sandbox-image" / "Dockerfile.slim"
HOSTED_DEPLOY_SERVICE = (
    REPO_ROOT / "scripts" / "systemd" / "matrx-hosted-deploy.service"
)
HOSTED_RUNNER = REPO_ROOT / "scripts" / "systemd" / "pull-deploy-runner.sh"
NESTED_PREFIX = "infra/matrx-sandbox"
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
    archive = subprocess.run(
        ["git", "archive", legacy_sha, "orchestrator"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["tar", "-x", "--strip-components=1", "-C", str(destination)],
        input=archive.stdout,
        check=True,
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
    assert 'APPROVAL_REF="${MATRX_SANDBOX_APPROVAL_PREFIX}$GITHUB_SHA"' in approval
    assert 'release_source_init "$PWD"' in approval
    assert "git push --atomic" in approval
    assert "--force-with-lease=refs/heads/main:$GITHUB_SHA origin" in approval
    assert '"${GITHUB_SHA}:refs/heads/main"' in approval
    assert '"${GITHUB_SHA}:${APPROVAL_REF}"' in approval
    assert 'APPROVAL_REF="${MATRX_SANDBOX_APPROVAL_PREFIX}$GITHUB_SHA"' in revalidation
    assert 'release_source_init "$PWD"' in revalidation
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


def test_hosted_aidream_freshness_never_falls_back_to_tracking_ref():
    deploy = HOSTED_DEPLOY.read_text(encoding="utf-8")
    resolver = deploy[
        deploy.index("resolve_aidream_source_sha()") : deploy.index("aidream_stale()")
    ]
    stale = deploy[
        deploy.index("aidream_stale()") : deploy.index(
            "# ── Resolve OLD/NEW commit", deploy.index("aidream_stale()")
        )
    ]

    assert "ls-remote origin refs/heads/main" in resolver
    assert "refs/remotes/origin/main" not in resolver
    assert "refusing a stale tracking-ref fallback" in stale
    assert 'if [ -z "$remote" ]' in stale
    assert "return 0" in stale


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
    assert "MATRX_TEMP_DIR=/tmp/aidream-managed" in autostart
    assert "LOG_DIR=/var/log/aidream" in autostart
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
    assert "find /opt/aidream-template -writable -print -quit' agent" in dockerfile
    assert '"${AIDREAM_TEMPLATE_DIR}/run.py"' in dockerfile
    assert '"${AIDREAM_TEMPLATE_DIR}/config/settings.py"' in dockerfile
    assert '"${AIDREAM_TEMPLATE_DIR}/aidream/settings/__init__.py"' in dockerfile
    assert 'RUN chmod go-w' in dockerfile
    assert "test ! -w /opt/aidream-template/pyproject.toml" in dockerfile
    assert 'chown -R root:root "${AIDREAM_TEMPLATE_DIR}"' not in dockerfile
    assert 'chmod -R a-w "${AIDREAM_TEMPLATE_DIR}"' not in dockerfile
    assert 'git config --system --add safe.directory "${AIDREAM_TEMPLATE_DIR}"' in dockerfile
    assert "mkdir -p /var/log/aidream" in dockerfile
    assert "chown agent:agent /var/log/aidream" in dockerfile


def test_aidream_build_proves_agent_cannot_mutate_certified_runtime():
    builder = AIDREAM_BUILDER.read_text(encoding="utf-8")
    bootstrap = AIDREAM_MANAGED_BOOTSTRAP.read_text(encoding="utf-8")

    assert "docker run --rm --read-only" in builder
    assert "--user root" in builder
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
    assert 'cp -a /opt/aidream-template/. "$runtime_dir/"' not in builder
    assert "touch /var/log/aidream/.agent-log-probe" in builder
    assert "sitecustomize.py" in builder
    assert "fsmonitor = !touch /tmp/gitconfig-ran" in builder
    assert "test ! -e /tmp/sitecustomize-ran" in builder
    assert "test ! -e /tmp/gitconfig-ran" in builder
    assert "for shim in findmnt sudo env sleep bash" in builder
    assert "find /tmp -maxdepth 1 -name 'shim-*-ran'" in builder
    assert "--tmpfs /run:rw,nosuid,nodev,mode=1777" in builder
    assert "prepare-aidream-managed-home.py" in builder
    assert "MATRX_TEMP_DIR=/tmp/aidream-managed LOG_DIR=/var/log/aidream" in builder
    assert "/opt/sandbox/scripts/aidream-managed-bootstrap.py" in builder
    assert "--verify-imports" in builder
    assert "runpy.run_path" in bootstrap
    assert 'sys.path.insert(0, os.fspath(TEMPLATE_ROOT))' in bootstrap


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
    managed_branch = helper[
        helper.index('if [ "$require_image_source" -eq 1 ]; then') : helper.index(
            "    else", helper.index('if [ "$require_image_source" -eq 1 ]; then')
        )
    ]

    assert 'if [ "$require_image_source" -eq 1 ]' in helper
    assert 'nohup "$MANAGED_TEMPLATE_DIR/.venv/bin/python" -I -B "$MANAGED_BOOTSTRAP"' in managed_branch
    assert "uv run" not in managed_branch


def test_managed_aidream_bootstrap_executes_isolated_real_import_chain():
    completed = subprocess.run(
        ["bash", str(AIDREAM_MANAGED_BOOTSTRAP_TEST)],
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_managed_aidream_home_rejects_unsafe_existing_paths():
    completed = subprocess.run(
        ["bash", str(AIDREAM_MANAGED_HOME_TEST)], text=True, capture_output=True
    )
    assert completed.returncode == 0, completed.stderr


def test_exact_release_verifier_rejects_untracked_tampering():
    helper = AIDREAM_HELPER.read_text(encoding="utf-8")

    assert "status --porcelain --untracked-files=all" in helper


def test_every_aidream_container_path_uses_shared_isolation_and_readiness():
    manager = (REPO_ROOT / "orchestrator/orchestrator/sandbox_manager.py").read_text()
    migrate = (REPO_ROOT / "orchestrator/orchestrator/migrate.py").read_text()
    pool = (REPO_ROOT / "orchestrator/orchestrator/pool.py").read_text()

    assert "**container_runtime_isolation(template, location.tier)" in manager
    from orchestrator.hosted_runtime import replacement_config

    old = SimpleNamespace(
        id="0123456789abcdef",
        attrs={
            "Config": {
                "Cmd": ["/opt/sandbox/scripts/entrypoint.sh"],
                "Entrypoint": ["/bin/sh", "-ec"],
                "User": "agent",
                "WorkingDir": "/home/agent",
                "Env": ["OLD=1"],
                "Volumes": {},
            },
            "HostConfig": {
                "Binds": ["home:/home/agent:rw"],
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Tmpfs": {"/tmp": "rw,noexec"},
            },
            "Mounts": [{"Type": "volume", "Name": "home", "Destination": "/home/agent", "RW": True}],
            "NetworkSettings": {"Networks": {"bridge": {"Aliases": ["sandbox"]}}},
        },
    )
    config = replacement_config(old, image="sha256:" + "a" * 64, environment=["NEW=1"], operation="proof")
    assert config["HostConfig"]["ReadonlyRootfs"] is True
    assert config["HostConfig"]["CapDrop"] == ["ALL"]
    assert config["HostConfig"]["SecurityOpt"] == ["no-new-privileges"]
    assert config["HostConfig"]["Tmpfs"] == {"/tmp": "rw,noexec"}
    assert config["Cmd"] == old.attrs["Config"]["Cmd"]
    assert config["Entrypoint"] == old.attrs["Config"]["Entrypoint"]
    assert config["User"] == "agent" and config["WorkingDir"] == "/home/agent"
    assert '_wait_container_ready(new, verify_timeout, template)' in migrate
    assert "http://127.0.0.1:8001/api/health/ready" in migrate
    assert "aidream-helpers.sh verify-release" in migrate
    # The warm pool is retired: the file must no longer be able to BOOT a box
    # (that is what made a sentinel-identity container in the first place).
    assert "containers.run(" not in pool
    assert "retire_warm_pool" in pool


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


def test_deployment_scripts_never_bulk_prune_containers():
    """A deploy must not delete stopped containers that retain sandbox homes."""
    forbidden = re.compile(r"^\s*docker\s+(?:system|container)\s+prune\b", re.MULTILINE)
    violations = {
        script.relative_to(REPO_ROOT): forbidden.findall(script.read_text(encoding="utf-8"))
        for script in sorted((REPO_ROOT / "scripts").glob("deploy-*.sh"))
    }
    assert all(not matches for matches in violations.values()), violations


def test_ec2_release_requires_the_private_aidream_replica_before_and_after_swap():
    script = (REPO_ROOT / "scripts" / "deploy-ec2.sh").read_text(encoding="utf-8")
    expected = 'EXPECTED_AIDREAM_URL="http://aidream.internal.matrxserver.com"'
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
    assert post_swap.count("\n  rollback\n") == 2
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


def test_hosted_promotion_holds_exclusive_peer_of_migration_lock():
    """A release cannot stop the orchestrator during backup/CAS/recovery."""
    script = (REPO_ROOT / "scripts" / "deploy-hosted.sh").read_text(encoding="utf-8")
    acquire = script.index('start_migration_lock_holder named "$DEPLOY_LOCK_WAIT_SECONDS" deployment')
    stop = script.index("docker compose stop", acquire)
    audit = script.index('audit_migration_release_state "$AUDIT_IMAGE"', acquire)
    verified = script.index('log "release contract verified', stop)
    release = script.index("release_migration_lock_holder", verified)
    assert acquire < audit < stop < verified < release

    # THE 2026-09-14 RULING: the bootstrap path must NEVER pause the live
    # orchestrator before it has SEIZED the locks. Pausing first meant a
    # contended or invalid seize froze a healthy edge for an attempt that was
    # then rolled back. Re-censusing the lock set after the freeze replaces what
    # the earlier pause bought. Behavioural guard (the real bash, faked docker):
    # tests/test_hosted_promotion_lock_order.py.
    marker = script.index("# Bootstrap from lockless 474", acquire)
    assert "bootstrap_barrier_seize" in script[marker : script.index("\nfi\n", marker)]
    body_start = script.index("bootstrap_barrier_seize() {")
    body = script[body_start : script.index("\n}\n", body_start)]
    old_locks = body.index('acquire_frozen_old_locks "$AUDIT_IMAGE"')
    pause = body.index('docker pause "$BOOTSTRAP_OLD_ID"')
    census = body.index('[ "$(lock_census)" = "$seized_census" ]')
    bootstrap_audit = body.index('audit_migration_release_state "$AUDIT_IMAGE"')
    fatal = body.index('docker kill --signal KILL "$BOOTSTRAP_OLD_ID"')
    assert old_locks < pause < census < bootstrap_audit < fatal
    promote = script.index('for index in "${!LIVE_TAGS[@]}"', marker)
    assert marker < promote
    lock_helper = (
        REPO_ROOT / "orchestrator/orchestrator/release_lock_holder.py"
    ).read_text(encoding="utf-8")
    assert "os.O_NOFOLLOW | os.O_NONBLOCK" in lock_helper
    assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in lock_helper
    assert "deployment_migration_barrier" in script

    runtime = (REPO_ROOT / "orchestrator/orchestrator/hosted_runtime.py").read_text(
        encoding="utf-8"
    )
    assert runtime.count('journal.lock("deployment", shared=True)') == 2



# ── Release source layout: standalone repository today, aidream monorepo later ──
# Every deploy path derives its ref names from the checkout layout, the EC2 host
# fetches with the deploy job's own token, and a history-rewriting import keeps
# the live hosts' recorded SHAs meaningful through scripts/release-lineage.tsv.


def _monorepo_history(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    """A bare origin holding a monorepo with the sandbox at NESTED_PREFIX."""
    origin = tmp_path / "mono.git"
    author = tmp_path / "mono-author"
    checkout = tmp_path / "mono-checkout"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "-b", "main", str(author))
    _git(author, "config", "user.email", "release-test@example.com")
    _git(author, "config", "user.name", "Release Test")
    sandbox = author / NESTED_PREFIX / "scripts" / "lib"
    sandbox.mkdir(parents=True)
    shutil.copy(RELEASE_GUARD, sandbox / "release-guard.sh")
    (author / "apps").mkdir()
    (author / "apps" / "other.txt").write_text("one\n", encoding="utf-8")
    _git(author, "add", "-A")
    _git(author, "commit", "-m", "first")
    first = _git(author, "rev-parse", "HEAD")
    (author / NESTED_PREFIX / "release.txt").write_text("two\n", encoding="utf-8")
    _git(author, "add", "-A")
    _git(author, "commit", "-m", "second")
    second = _git(author, "rev-parse", "HEAD")
    _git(author, "remote", "add", "origin", str(origin))
    _git(author, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", "--branch", "main", str(origin), str(checkout))
    return checkout, author, origin, first, second


def _layout(root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    script = """
set -u
fail() { echo "$*" >&2; exit 1; }
source "$1"
release_source_init "$2"
printf '%s\\n' "$MATRX_SANDBOX_SUBDIR" "$MATRX_SANDBOX_DEPLOY_REF" "$MATRX_SANDBOX_APPROVAL_PREFIX"
"""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("MATRX_SANDBOX_")}
    return subprocess.run(
        ["bash", "-c", script, "layout-test", str(RELEASE_GUARD), str(root)],
        text=True,
        capture_output=True,
        env={**clean, **env},
    )


def test_release_ref_names_follow_the_checkout_layout(tmp_path: Path):
    (tmp_path / "root").mkdir()
    root_checkout, _, _, _ = _release_history(tmp_path / "root")
    mono_checkout, _, _, _, _ = _monorepo_history(tmp_path)

    root = _layout(root_checkout)
    nested = _layout(mono_checkout / NESTED_PREFIX)
    mismatch = _layout(mono_checkout / NESTED_PREFIX, MATRX_SANDBOX_SUBDIR="infra/other")
    bad_ref = _layout(root_checkout, MATRX_SANDBOX_DEPLOY_REF="refs/tags/x")

    assert root.returncode == 0, root.stderr
    assert root.stdout.splitlines() == [
        "",
        "refs/heads/deploy/hosted",
        "refs/tags/deploy-approved/",
    ]
    assert nested.returncode == 0, nested.stderr
    assert nested.stdout.splitlines() == [
        NESTED_PREFIX,
        "refs/heads/deploy/sandbox-hosted",
        "refs/tags/sandbox/deploy-approved/",
    ]
    assert mismatch.returncode != 0 and "MATRX_SANDBOX_SUBDIR" in mismatch.stderr
    assert bad_ref.returncode != 0 and "refs/heads/" in bad_ref.stderr


def test_host_runner_defaults_match_the_guard_layout_rule():
    runner = HOSTED_RUNNER.read_text(encoding="utf-8")
    guard = RELEASE_GUARD.read_text(encoding="utf-8")

    for ref in ("refs/heads/deploy/hosted", "refs/heads/deploy/sandbox-hosted"):
        assert f"MATRX_SANDBOX_DEPLOY_REF:-{ref}" in runner
        assert f"MATRX_SANDBOX_DEPLOY_REF:={ref}" in guard
    assert "/etc/matrx/sandbox-release.env" in runner
    assert 'MATRX_SANDBOX_DIR="$RUNNER_SANDBOX_ROOT"' in runner


def _approval_step_script() -> str:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("- name: Promote tested commit to hosted approval ref")
    body = workflow[workflow.index("run: |\n", start) + len("run: |\n") : workflow.index("\n  deploy:")]
    return textwrap.dedent(body)


def _run_approval(cwd: Path, sha: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("MATRX_SANDBOX_")}
    return subprocess.run(
        ["bash", "-e", "-c", _approval_step_script()],
        cwd=cwd,
        text=True,
        capture_output=True,
        env={**clean, "GITHUB_SHA": sha},
    )


def test_monorepo_approval_ignores_unrelated_main_moves_only(tmp_path: Path):
    checkout, author, origin, _, second = _monorepo_history(tmp_path)
    sandbox = checkout / NESTED_PREFIX

    # Unrelated monorepo work lands after the tested commit: still approvable.
    (author / "apps" / "other.txt").write_text("moved\n", encoding="utf-8")
    _git(author, "commit", "-am", "unrelated")
    _git(author, "push", "origin", "main")
    approved = _run_approval(sandbox, second)
    refs = _git(origin, "for-each-ref", "--format=%(refname) %(objectname)")

    assert approved.returncode == 0, approved.stderr
    assert f"refs/heads/deploy/sandbox-hosted {second}" in refs
    assert f"refs/tags/sandbox/deploy-approved/{second} {second}" in refs
    assert "refs/heads/deploy/hosted" not in refs

    # The sandbox tree itself moves past the tested commit: refuse.
    (author / NESTED_PREFIX / "release.txt").write_text("three\n", encoding="utf-8")
    _git(author, "commit", "-am", "sandbox change")
    _git(author, "push", "origin", "main")
    third = _git(author, "rev-parse", "HEAD")
    _git(checkout, "fetch", "origin", "main")
    stale = _run_approval(sandbox, second)
    assert stale.returncode != 0
    assert f"{NESTED_PREFIX} moved on main" in stale.stderr

    # The newer sandbox commit then approves forward from the leased deploy ref.
    _git(checkout, "checkout", "-q", "--detach", third)
    forward = _run_approval(sandbox, third)
    assert forward.returncode == 0, forward.stderr
    assert _git(origin, "rev-parse", "refs/heads/deploy/sandbox-hosted") == third


def test_standalone_approval_still_requires_current_main(tmp_path: Path):
    checkout, author, first, second = _release_history(tmp_path)
    (checkout / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(RELEASE_GUARD, checkout / "scripts" / "lib" / "release-guard.sh")

    historical = _run_approval(checkout, first)
    current = _run_approval(checkout, second)

    assert historical.returncode != 0
    assert "main has moved" in historical.stderr
    assert current.returncode == 0, current.stderr
    assert _git(author, "ls-remote", "origin", "refs/heads/deploy/hosted").startswith(second)


def test_nested_guard_uses_monorepo_approval_ref_and_lineage(tmp_path: Path):
    checkout, author, _, first, second = _monorepo_history(tmp_path)
    sandbox = checkout / NESTED_PREFIX
    _git(author, "push", "origin", f"{second}:refs/tags/sandbox/deploy-approved/{second}")
    pre_move = "e" * 40  # a SHA the live host recorded before a rewriting import

    approved = _run_approved_guard(sandbox, first, second)
    unmapped = _run_approved_guard(sandbox, pre_move, second)
    lineage = sandbox / "scripts" / "release-lineage.tsv"
    lineage.write_text(f"{pre_move}\t{first}\n", encoding="utf-8")
    mapped = _run_approved_guard(sandbox, pre_move, second)
    lineage.write_text(f"{pre_move}\t{second}\n{pre_move}\t{first}\n", encoding="utf-8")
    ambiguous = _run_approved_guard(sandbox, pre_move, second)

    assert approved.returncode == 0, approved.stderr
    assert unmapped.returncode != 0
    assert "unavailable in release history" in unmapped.stderr
    assert mapped.returncode == 0, mapped.stderr
    assert "lineage maps it to" in mapped.stderr
    assert ambiguous.returncode != 0


def test_hosted_change_set_is_relative_to_the_sandbox_root():
    deploy = HOSTED_DEPLOY.read_text(encoding="utf-8")
    assert 'git diff --name-only --relative "$OLD_SHA" "$NEW_SHA"' in deploy
    assert 'release_source_init "$REPO_DIR"' in deploy
    assert "refs/heads/deploy/hosted" not in deploy
    # Image labels written before a rewriting import stay "compatible" through lineage.
    assert 'actual=$(release_guard_resolve_lineage "$REPO_DIR" "$actual" 2>/dev/null)' in deploy


def test_ec2_deploy_fetches_with_the_job_token_never_anonymously():
    workflows = sorted(DEPLOY_WORKFLOW.parent.glob("*.yml"))
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        assert "github.com/armanisadeghi/" not in text, path.name
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    ssm = workflow[workflow.index("- name: Deploy to EC2 via SSM") : workflow.index("COMMAND_ID=")]

    assert "GIT_AUTH_TOKEN: ${{ github.token }}" in ssm
    assert 'REPO_URL="https://github.com/${GITHUB_REPOSITORY}.git"' in ssm
    assert 'echo "::add-mask::$AUTH_B64"' in ssm
    assert 'echo "SSM Parameters: ${PARAMS//$AUTH_B64/***}"' in ssm
    assert 'echo "SSM Parameters: $PARAMS"' not in ssm
    assert "fetch --filter=blob:none origin" in ssm
    # The credential line runs before xtrace so the SSM transcript never echoes it.
    assert ssm.index("GIT_CONFIG_VALUE_0=") < ssm.index('"set -x"')


def test_hosted_fast_path_runs_the_installed_poller_runner():
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    hosted_job = workflow[workflow.index("  deploy-hosted:") :]
    assert "/usr/local/bin/matrx-hosted-deploy-runner" in hosted_job
    assert "MATRX_SANDBOX_EXPECT_SHA='${{ github.sha }}'" in hosted_job
    assert "/srv/projects/matrx-sandbox" not in hosted_job


def test_host_runner_syncs_the_nested_checkout_end_to_end(tmp_path: Path):
    if shutil.which("flock") is None:
        # CI (ubuntu) has util-linux flock; a bare macOS shell does not.
        import pytest

        pytest.skip("flock is not installed on this machine; CI runs this test")
    checkout, author, _, _, second = _monorepo_history(tmp_path)
    stub = author / NESTED_PREFIX / "scripts" / "deploy-hosted.sh"
    stub.write_text(
        '#!/usr/bin/env bash\nprintf "%s|%s|%s\\n" "$MATRX_SANDBOX_DIR" '
        '"$DEPLOY_TARGET_SHA" "$MATRX_SANDBOX_DEPLOY_REF" > "$RUNNER_PROOF"\n',
        encoding="utf-8",
    )
    _git(author, "add", "-A")
    _git(author, "commit", "-m", "stub deploy")
    target = _git(author, "rev-parse", "HEAD")
    _git(author, "push", "origin", f"{target}:refs/heads/deploy/sandbox-hosted")
    env_file = tmp_path / "sandbox-release.env"
    env_file.write_text(
        f"MATRX_SANDBOX_CHECKOUT={checkout}\nMATRX_SANDBOX_SUBDIR={NESTED_PREFIX}\n",
        encoding="utf-8",
    )
    proof = tmp_path / "proof.txt"
    clean = {k: v for k, v in os.environ.items() if not k.startswith("MATRX_SANDBOX_")}
    env = {
        **clean,
        "MATRX_SANDBOX_RELEASE_ENV": str(env_file),
        "MATRX_SANDBOX_LOCK_DIR": str(tmp_path / "locks"),
        "RUNNER_PROOF": str(proof),
    }

    run = subprocess.run(["bash", str(HOSTED_RUNNER)], env=env, text=True, capture_output=True)
    wrong = subprocess.run(
        ["bash", str(HOSTED_RUNNER)],
        env={**env, "MATRX_SANDBOX_EXPECT_SHA": second},
        text=True,
        capture_output=True,
    )

    assert run.returncode == 0, run.stderr
    assert proof.read_text(encoding="utf-8").strip() == (
        f"{checkout / NESTED_PREFIX}|{target}|refs/heads/deploy/sandbox-hosted"
    )
    assert _git(checkout, "rev-parse", "HEAD") == target
    assert wrong.returncode != 0
    assert "caller expected" in wrong.stderr
