"""Which downstream entrypoint a hosted aidream box hands off to.

The regression this closes, in full: `entrypoint-aidream.sh` chose the
PRODUCTION entrypoint whenever `S3_BUCKET` was non-empty, on the premise that
"the hosted tier doesn't set it". The hosted orchestrator sets it on every
container (`sandbox_manager.py`: `"S3_BUCKET": location.s3_bucket or ""`), so
every hosted aidream box ran the EC2 hot-sync/cold-mount path and exited 1 on
`mount point /data/cold is already mounted`. Boxes read `failed` with the real
cause buried in the docker log; the last hosted aidream box that came up was
2026-09-13, five days before anyone tried again (2026-09-18, sbx-9a6aeaba3be8,
lane XT-09's own verification box).

The decision was untestable where it lived — the script seeds 2.7 GB before
reaching it — so it now lives in `aidream-downstream-entrypoint.sh`, which
prints its answer and nothing else. These are the cases.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DECIDER = REPO_ROOT / "sandbox-image" / "scripts" / "aidream-downstream-entrypoint.sh"


def _script_dir(tmp_path: Path, *, production: bool = True, local: bool = True) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name, present in (("entrypoint.sh", production), ("entrypoint-local.sh", local)):
        if present:
            target = scripts / name
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)
    return scripts


def _decide(scripts: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DECIDER)],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ["PATH"], "AIDREAM_SCRIPT_DIR": str(scripts), **env},
    )


def test_hosted_tier_never_takes_the_s3_path_even_with_s3_bucket_set(tmp_path: Path) -> None:
    """THE regression. This exact environment is what the hosted tier produces."""
    scripts = _script_dir(tmp_path)
    result = _decide(
        scripts,
        MATRX_TIER="hosted",
        S3_BUCKET="matrx-sandbox-storage-prod-2024",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(scripts / "entrypoint-local.sh")


def test_hosted_tier_without_the_local_script_refuses_loudly(tmp_path: Path) -> None:
    # Never a silent fall-through to the S3 path: on the hosted tier it cannot
    # work, and the failure it produces names a mount instead of the real cause.
    scripts = _script_dir(tmp_path, local=False)
    result = _decide(scripts, MATRX_TIER="hosted", S3_BUCKET="bucket")
    assert result.returncode != 0
    assert "tier=hosted" in result.stderr
    assert "entrypoint-local.sh" in result.stderr
    assert result.stdout.strip() == ""


def test_ec2_tier_with_s3_still_takes_the_production_path(tmp_path: Path) -> None:
    scripts = _script_dir(tmp_path)
    result = _decide(scripts, MATRX_TIER="ec2", S3_BUCKET="bucket")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(scripts / "entrypoint.sh")


def test_no_tier_and_no_s3_is_the_local_image(tmp_path: Path) -> None:
    scripts = _script_dir(tmp_path)
    result = _decide(scripts)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(scripts / "entrypoint-local.sh")


def test_nothing_available_refuses_instead_of_guessing(tmp_path: Path) -> None:
    scripts = _script_dir(tmp_path, production=False, local=False)
    result = _decide(scripts)
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_the_boot_script_delegates_the_decision_and_never_reimplements_it() -> None:
    """A second copy of the branch is how the two discriminators diverged."""
    boot = (REPO_ROOT / "sandbox-image" / "scripts" / "entrypoint-aidream.sh").read_text()
    assert "aidream-downstream-entrypoint.sh" in boot
    # No inline hand-off decision left behind.
    assert "exec /opt/sandbox/scripts/entrypoint.sh" not in boot
    assert "exec /opt/sandbox/scripts/entrypoint-local.sh" not in boot


def test_the_decider_ships_in_the_image() -> None:
    dockerfile = (REPO_ROOT / "sandbox-image" / "Dockerfile.aidream").read_text()
    assert "aidream-downstream-entrypoint.sh" in dockerfile, (
        "the boot script execs it, so the image must COPY it — otherwise every "
        "aidream box fails at hand-off"
    )


@pytest.mark.parametrize("tier", ["hosted", "HOSTED ", " hosted"])
def test_only_the_exact_tier_token_counts(tmp_path: Path, tier: str) -> None:
    # An orchestrator that ever sends a differently-cased or padded value must
    # not silently land on the production path; it lands on the no-tier branch,
    # which is the local script, not the S3 one... unless S3_BUCKET is set, in
    # which case the box would break again. So assert the strict match here and
    # keep the orchestrator's own value canonical.
    scripts = _script_dir(tmp_path)
    result = _decide(scripts, MATRX_TIER=tier)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(scripts / "entrypoint-local.sh")
