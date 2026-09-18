"""A boot step never reports work it did not do.

The regression, in full (feedback 81cb4265, measured 2026-09-18): the editable
`.pth` retarget inside `entrypoint-aidream.sh` counted LOOP ITERATIONS, not
successful rewrites, and ran under `set -uo pipefail` with no `-e`. On a
persistent volume holding an older root-owned seed, `sed -i` failed seventeen
times with

    /bin/sed: couldn't open temporary file /home/agent/aidream/.venv/lib/
    python3.13/site-packages/sedXXXXXX: Permission denied

and the step still logged `retargeted 17 editable .pth file(s)`. Every
`import matrx_ai` from the user's checkout kept resolving to the immutable
template and the log said the opposite. The second, quieter half of the same bug:
the site-packages path was hard-coded to `python3.13`, so any other venv version
took the "no site-packages" return and logged a clean-looking skip.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "sandbox-image" / "scripts" / "aidream-retarget-editables.sh"

TEMPLATE_DIR = "/opt/aidream-template"


def _work_dir(tmp_path: Path, *, python: str = "python3.13", count: int = 3) -> Path:
    work = tmp_path / "aidream"
    site = work / ".venv" / "lib" / python / "site-packages"
    site.mkdir(parents=True)
    for index in range(count):
        (site / f"_editable_impl_pkg{index}.pth").write_text(
            f"import sys; sys.path.insert(0, {TEMPLATE_DIR!r} + '/packages/pkg{index}')\n"
        )
    return work


def _site(work: Path) -> Path:
    return next((work / ".venv" / "lib").glob("python*")) / "site-packages"


def _run(work: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), TEMPLATE_DIR, str(work)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ["PATH"]},
    )


def test_a_writable_seed_is_retargeted_and_the_count_is_the_truth(tmp_path: Path) -> None:
    work = _work_dir(tmp_path)
    result = _run(work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "retargeted 3 of 3" in result.stdout
    for pth in _site(work).glob("_editable_impl_*.pth"):
        text = pth.read_text()
        assert TEMPLATE_DIR not in text
        assert str(work) in text


def test_an_unwritable_seed_refuses_instead_of_claiming_success(tmp_path: Path) -> None:
    """THE regression. Before the fix this printed "retargeted 3" and exited 0."""
    work = _work_dir(tmp_path)
    site = _site(work)
    site.chmod(0o555)  # sed -i cannot create its temp file in here
    try:
        result = _run(work)
    finally:
        site.chmod(0o755)
    assert result.returncode == 3, result.stdout + result.stderr
    # It must NOT say it retargeted three files.
    assert "retargeted 3 of 3" not in result.stdout
    assert "retargeted 0 of 3" in result.stderr
    assert "3 still point at /opt/aidream-template" in result.stderr
    assert "REMEDY" in result.stderr
    # Every failing path is named, not counted.
    for index in range(3):
        assert f"_editable_impl_pkg{index}.pth" in result.stderr
    # And the files really were left alone — the report matches the disk.
    for pth in site.glob("_editable_impl_*.pth"):
        assert TEMPLATE_DIR in pth.read_text()


def test_a_venv_on_another_python_version_is_not_silently_skipped(tmp_path: Path) -> None:
    # The old code hard-coded python3.13 and returned "no site-packages" —
    # a skip that reads like a no-op while every import stayed wrong.
    work = _work_dir(tmp_path, python="python3.14")
    result = _run(work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "retargeted 3 of 3" in result.stdout
    for pth in _site(work).glob("_editable_impl_*.pth"):
        assert TEMPLATE_DIR not in pth.read_text()


def test_already_correct_files_are_reported_as_such(tmp_path: Path) -> None:
    work = _work_dir(tmp_path)
    assert _run(work).returncode == 0
    second = _run(work)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "retargeted 0 of 3" in second.stdout
    assert "(3 already correct)" in second.stdout


def test_no_venv_at_all_says_so_and_succeeds(tmp_path: Path) -> None:
    work = tmp_path / "aidream"
    work.mkdir()
    result = _run(work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no .venv site-packages" in result.stdout


def test_a_venv_with_no_editables_says_so_and_succeeds(tmp_path: Path) -> None:
    work = _work_dir(tmp_path, count=0)
    result = _run(work)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no editable .pth files" in result.stdout


def test_missing_arguments_refuse(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 2
    assert "usage" in result.stderr
