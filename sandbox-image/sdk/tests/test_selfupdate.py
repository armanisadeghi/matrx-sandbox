"""Guards for the in-container SDK installer (``mtx self-update``).

These run the REAL installer against real directories — no mocked filesystem —
because the whole risk of this primitive is that it writes in the wrong place.
The load-bearing claims: the user's home is never touched, the swap is atomic
enough that a live import never sees a half-tree, the daemon is only restarted
when the daemon's own code changed, and an old box gains the new commands.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from matrx_agent import selfupdate  # noqa: E402


def _write(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _make_tree(root: str, *, cli_body: str, api_body: str, extra: dict | None = None) -> None:
    _write(os.path.join(root, "matrx_agent", "api", "main.py"), api_body)
    _write(os.path.join(root, "matrx_agent", "cli", "__main__.py"), cli_body)
    _write(os.path.join(root, "pyproject.toml"), 'dependencies = [\n  "httpx>=0.25",\n]\n')
    for rel, body in (extra or {}).items():
        _write(os.path.join(root, rel), body)


def _digest_dir(root: str) -> dict:
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = (
                    hashlib.sha256(fh.read()).hexdigest(),
                    oct(os.stat(full).st_mode),
                )
    return out


@pytest.fixture()
def boxes(tmp_path, monkeypatch):
    """An /opt/sandbox with an old SDK, a staged new one, and a user home."""
    opt = tmp_path / "opt" / "sandbox"
    target = opt / "sdk"
    source = opt / "sdk.incoming"
    home = tmp_path / "home" / "agent"
    _make_tree(
        str(target),
        cli_body="# august CLI: no toolchain command\n",
        api_body="# august daemon\n",
        extra={"matrx_agent/cli/files.py": "# files\n"},
    )
    _make_tree(
        str(source),
        cli_body="# september CLI\ndef toolchain_ensure():\n    return 'ensured'\n",
        api_body="# august daemon\n",
        extra={"matrx_agent/cli/toolchain.py": "# the new command\n"},
    )
    _write(str(home / "work" / "notes.md"), "the user's actual work\n")
    _write(str(home / ".bashrc"), "export PATH=$PATH\n")
    monkeypatch.setattr(selfupdate, "MTX_SHIM", str(tmp_path / "usr" / "local" / "bin" / "mtx"))
    os.makedirs(os.path.dirname(selfupdate.MTX_SHIM), exist_ok=True)
    return {"opt": str(opt), "target": str(target), "source": str(source), "home": str(home)}


def test_an_old_box_gains_the_new_command_and_the_home_is_untouched(boxes):
    before = _digest_dir(boxes["home"])

    result = selfupdate.apply(
        boxes["source"], boxes["target"], image_id="sha256:current", image_version="sep14"
    )

    assert result["status"] == "refreshed", result
    assert os.path.isfile(os.path.join(boxes["target"], "matrx_agent", "cli", "toolchain.py"))
    with open(os.path.join(boxes["target"], "matrx_agent", "cli", "__main__.py")) as fh:
        assert "september" in fh.read()
    # THE home guarantee — byte-for-byte, modes included.
    assert _digest_dir(boxes["home"]) == before
    # The stamp is what rate-limits the next binding.
    assert selfupdate.read_stamp(boxes["target"])["to_image_id"] == "sha256:current"
    assert selfupdate.installed_version(boxes["target"]) == "sep14"
    # The previous tree is kept exactly once, as the rollback.
    assert os.path.isfile(os.path.join(boxes["target"] + ".prev", "matrx_agent", "cli", "files.py"))
    assert not os.path.exists(boxes["target"] + ".staging")


def test_an_unchanged_daemon_is_never_restarted(boxes, monkeypatch):
    called = []
    monkeypatch.setattr(selfupdate, "restart_daemon", lambda *a, **k: called.append(1) or {"status": "restarted"})

    result = selfupdate.apply(
        boxes["source"], boxes["target"], image_version="sep14", allow_daemon_restart=True
    )

    assert result["status"] == "refreshed"
    assert result["daemon_restart"]["status"] == "not_needed"
    assert called == [], "only the CLI changed — a live terminal must not be dropped for that"


def test_a_changed_daemon_defers_its_restart_unless_allowed(boxes, monkeypatch):
    _write(os.path.join(boxes["source"], "matrx_agent", "api", "main.py"), "# september daemon\n")
    monkeypatch.setattr(selfupdate, "restart_daemon", lambda *a, **k: {"status": "restarted"})

    deferred = selfupdate.apply(boxes["source"], boxes["target"], image_version="sep14")
    assert deferred["daemon_restart"]["status"] == "deferred"
    assert "terminal" in deferred["daemon_restart"]["reason"]

    # A second box, same change, with no attached session: it restarts.
    _write(os.path.join(boxes["source"], "matrx_agent", "api", "main.py"), "# september daemon v2\n")
    allowed = selfupdate.apply(
        boxes["source"], boxes["target"], image_version="sep14", allow_daemon_restart=True
    )
    assert allowed["daemon_restart"]["status"] == "restarted"


def test_an_identical_tree_is_a_no_op(boxes):
    selfupdate.apply(boxes["source"], boxes["target"], image_id="i1", image_version="sep14")
    again = selfupdate.apply(boxes["source"], boxes["target"], image_id="i1", image_version="sep14")
    assert again["status"] == "already_current"


def test_a_new_dependency_is_reported_not_silently_skipped(boxes):
    _write(
        os.path.join(boxes["source"], "pyproject.toml"),
        'dependencies = [\n  "httpx>=0.25",\n  "anyio>=4",\n]\n',
    )
    result = selfupdate.apply(boxes["source"], boxes["target"], image_version="sep14")
    assert result["deps_checked"]["added"] == ["anyio"]


def test_it_refuses_to_install_into_a_home(tmp_path):
    with pytest.raises(selfupdate.RefuseToInstall):
        selfupdate.apply(str(tmp_path), "/home/agent/sdk")
    with pytest.raises(selfupdate.RefuseToInstall):
        selfupdate.apply(str(tmp_path), "/home/agent/.local/lib")


def test_a_junk_source_installs_nothing(boxes, tmp_path):
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "README").write_text("not an sdk")
    before = _digest_dir(boxes["target"])

    result = selfupdate.apply(str(junk), boxes["target"], image_version="sep14")

    assert result["status"] == "failed"
    assert "does not look like an SDK tree" in result["reason"]
    assert _digest_dir(boxes["target"]) == before


def test_the_cli_exposes_self_update():
    """`mtx self-update --help` must exist — that is the agent-side path."""
    root = os.path.join(os.path.dirname(__file__), "..")
    proc = subprocess.run(
        [sys.executable, "-m", "matrx_agent.cli", "self-update", "--help"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--allow-daemon-restart" in proc.stdout


def test_the_installer_runs_standalone_from_a_staged_tree(boxes):
    """The orchestrator runs the STAGED file by path on a box whose installed SDK
    may be ancient — so the module must not need its own package importable."""
    proc = subprocess.run(
        [
            sys.executable,
            os.path.join(os.path.dirname(__file__), "..", "matrx_agent", "selfupdate.py"),
            "--source", boxes["source"],
            "--target", boxes["target"],
            "--image-version", "sep14",
        ],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["status"] == "refreshed"
