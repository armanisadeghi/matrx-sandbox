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


def test_a_tree_that_cannot_be_renamed_is_still_replaced(boxes, monkeypatch):
    """On a box still running its original image the SDK sits in a lower
    overlayfs layer and CANNOT be renamed (EXDEV) — the case a live rehearsal
    caught. The swap falls back to copy-aside + replace, and says which it did."""
    import errno as _errno

    real_rename = os.rename
    tripped = []

    def _rename(src, dst):
        if src == boxes["target"] and not tripped:
            tripped.append(1)
            raise OSError(_errno.EXDEV, "Invalid cross-device link", src, None, dst)
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", _rename)
    before = _digest_dir(boxes["home"])

    result = selfupdate.apply(boxes["source"], boxes["target"], image_version="sep14")

    assert tripped, "the fallback was never exercised"
    assert result["status"] == "refreshed"
    assert result["swap_mode"] == "replace"
    assert os.path.isfile(os.path.join(boxes["target"], "matrx_agent", "cli", "toolchain.py"))
    assert os.path.isfile(os.path.join(boxes["target"] + ".prev", "matrx_agent", "cli", "files.py"))
    assert _digest_dir(boxes["home"]) == before


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


def test_a_daemon_that_will_not_come_up_is_rolled_back(boxes, monkeypatch):
    """The one way this primitive could really hurt a box: we stop a WORKING
    daemon and the new code cannot start (a dependency a file copy cannot
    install). The old tree goes back and the daemon comes back on it."""
    _write(os.path.join(boxes["source"], "matrx_agent", "api", "main.py"), "import nonexistent\n")
    monkeypatch.setattr(selfupdate, "_daemon_healthy", lambda *a, **k: True)
    restarts = []

    def _restart(*_a, **_k):
        restarts.append(1)
        return {"status": "failed" if len(restarts) == 1 else "restarted"}

    monkeypatch.setattr(selfupdate, "restart_daemon", _restart)

    result = selfupdate.apply(
        boxes["source"], boxes["target"], image_version="sep14", allow_daemon_restart=True
    )

    assert result["status"] == "rolled_back"
    assert result["rollback"]["status"] == "restored"
    # The box is back on the SDK it was working with.
    with open(os.path.join(boxes["target"], "matrx_agent", "api", "main.py")) as fh:
        assert fh.read() == "# august daemon\n"
    assert os.path.isfile(os.path.join(boxes["target"], "matrx_agent", "cli", "files.py"))
    assert len(restarts) == 2


def test_a_daemon_that_was_already_down_is_not_blamed_on_the_refresh(boxes, monkeypatch):
    _write(os.path.join(boxes["source"], "matrx_agent", "api", "main.py"), "# september daemon\n")
    monkeypatch.setattr(selfupdate, "_daemon_healthy", lambda *a, **k: False)
    monkeypatch.setattr(selfupdate, "restart_daemon", lambda *a, **k: {"status": "failed"})

    result = selfupdate.apply(
        boxes["source"], boxes["target"], image_version="sep14", allow_daemon_restart=True
    )

    assert result["status"] == "refreshed"
    assert "rollback" not in result


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


def test_on_demand_with_nothing_staged_names_the_remedy(tmp_path, boxes):
    """`mtx self-update` by hand is the normal empty case — only the orchestrator
    can reach the image. It must say what fixes it, not just fail."""
    result = selfupdate.apply(str(tmp_path / "nothing-here"), boxes["target"])
    assert result["status"] == "failed"
    assert "Rebind this sandbox" in result["reason"]


def test_an_existing_mtx_shim_is_never_rewritten(boxes):
    """A box whose shim points at another interpreter is a box where mtx WORKS.
    Correcting it would break the very command this refresh delivers."""
    with open(selfupdate.MTX_SHIM, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nexec /usr/local/bin/python3 -m matrx_agent.cli \"$@\"\n")

    result = selfupdate.apply(boxes["source"], boxes["target"], image_version="sep14")

    assert result["mtx_shim"] == "present"
    with open(selfupdate.MTX_SHIM, encoding="utf-8") as fh:
        assert "/usr/local/bin/python3 " in fh.read()


def test_a_box_born_without_the_shim_gets_one(boxes):
    result = selfupdate.apply(boxes["source"], boxes["target"], image_version="sep14")
    assert result["mtx_shim"] == "installed"
    assert os.access(selfupdate.MTX_SHIM, os.X_OK)


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


# ── 2026-10-17: dependencies are installed BEFORE the swap, or nothing swaps ──
# A staged tree that added ``matrx-api`` was swapped in, the daemon died on
# ModuleNotFoundError, the box rolled back after ~65 s — on every binding.

_LOCK = """\
httpx==0.28.1 \\
    --hash=sha256:aaaa
    # via
    #   matrx-api
    #   matrx-sdk
matrx-api==0.1.4 \\
    --hash=sha256:bbbb \\
    --hash=sha256:cccc
    # via matrx-sdk
attrs==25.1.0 \\
    --hash=sha256:dddd
    # via matrx-api
unrelated==1.0 \\
    --hash=sha256:eeee
    # via matrx-sdk
"""


def _add_matrx_api(boxes, *, with_lock=True):
    _write(
        os.path.join(boxes["source"], "pyproject.toml"),
        'dependencies = [\n  "httpx>=0.25",\n  "matrx-api>=0.1.4",\n]\n',
    )
    if with_lock:
        _write(os.path.join(boxes["source"], "requirements.lock"), _LOCK)


class _FakePython:
    """Stands in for the daemon's interpreter: what it has installed, and what
    a pip install does to that. Records the pip call and the target tree's state
    at that moment, so the test can prove the install came BEFORE the swap."""

    def __init__(self, boxes, *, pip_rc=0):
        self.boxes = boxes
        self.installed = {"httpx", "pydantic"}
        self.pip_rc = pip_rc
        self.pip_calls = []
        self.real_run = subprocess.run

    def dists(self, _python):
        return set(self.installed)

    def run(self, cmd, *args, **kwargs):
        if len(cmd) > 2 and cmd[1:4] == ["-m", "pip", "install"]:
            req = cmd[cmd.index("-r") + 1]
            with open(req, encoding="utf-8") as fh:
                body = fh.read()
            with open(os.path.join(self.boxes["target"], "matrx_agent", "cli", "__main__.py")) as fh:
                live_cli = fh.read()
            self.pip_calls.append({"cmd": cmd, "requirements": body, "live_cli": live_cli})
            if self.pip_rc == 0:
                for line in body.splitlines():
                    if line and not line[0].isspace():
                        self.installed.add(line.split("==")[0])
            return subprocess.CompletedProcess(cmd, self.pip_rc, "", "ERROR: no matching distribution")
        return self.real_run(cmd, *args, **kwargs)


def test_an_added_dependency_is_installed_from_the_lock_before_the_swap(boxes, monkeypatch):
    _add_matrx_api(boxes)
    fake = _FakePython(boxes)
    monkeypatch.setattr(selfupdate, "_installed_dists", fake.dists)
    monkeypatch.setattr(selfupdate.subprocess, "run", fake.run)

    result = selfupdate.apply(boxes["source"], boxes["target"], image_version="sep14")

    assert result["status"] == "refreshed", result
    assert result["deps_install"]["status"] == "installed"
    assert len(fake.pip_calls) == 1
    call = fake.pip_calls[0]
    assert "--require-hashes" in call["cmd"] and "--no-deps" in call["cmd"]
    # Only what is missing, with its pinned closure and its hashes — never a
    # re-version of what the daemon already has, never unrelated pins.
    assert "matrx-api==0.1.4" in call["requirements"]
    assert "--hash=sha256:bbbb" in call["requirements"]
    assert "attrs==25.1.0" in call["requirements"]
    assert "httpx" not in call["requirements"]
    assert "unrelated" not in call["requirements"]
    assert "august CLI" in call["live_cli"], "pip ran AFTER the swap — the crash window is back"


def test_an_uninstallable_dependency_aborts_before_anything_is_swapped(boxes, monkeypatch):
    _add_matrx_api(boxes)
    fake = _FakePython(boxes, pip_rc=1)
    monkeypatch.setattr(selfupdate, "_installed_dists", fake.dists)
    monkeypatch.setattr(selfupdate.subprocess, "run", fake.run)
    restarts = []
    monkeypatch.setattr(selfupdate, "restart_daemon", lambda *a, **k: restarts.append(1) or {})
    before = _digest_dir(boxes["target"])

    result = selfupdate.apply(
        boxes["source"], boxes["target"], image_version="sep14", allow_daemon_restart=True
    )

    assert result["status"] == "deps_missing", result
    assert "matrx-api" in result["reason"]
    assert _digest_dir(boxes["target"]) == before, "the live SDK changed on an aborted install"
    assert not os.path.exists(boxes["target"] + ".prev")
    assert restarts == [], "the daemon was touched on an aborted install"


def test_a_dependency_with_no_pin_aborts_instead_of_swapping(boxes, monkeypatch):
    _add_matrx_api(boxes, with_lock=False)
    fake = _FakePython(boxes)
    monkeypatch.setattr(selfupdate, "_installed_dists", fake.dists)
    monkeypatch.setattr(selfupdate.subprocess, "run", fake.run)
    before = _digest_dir(boxes["target"])

    result = selfupdate.apply(boxes["source"], boxes["target"], image_version="sep14")

    assert result["status"] == "deps_missing"
    assert fake.pip_calls == []
    assert _digest_dir(boxes["target"]) == before


def test_the_cli_exits_nonzero_on_deps_missing(boxes, monkeypatch, capsys):
    _add_matrx_api(boxes)
    fake = _FakePython(boxes, pip_rc=1)
    monkeypatch.setattr(selfupdate, "_installed_dists", fake.dists)
    monkeypatch.setattr(selfupdate.subprocess, "run", fake.run)
    rc = selfupdate.main(["--source", boxes["source"], "--target", boxes["target"]])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["status"] == "deps_missing"


def test_the_real_lock_parses_and_pins_matrx_api():
    lock = selfupdate.parse_lock(os.path.join(os.path.dirname(__file__), "..", "requirements.lock"))
    assert "matrx-api" in lock
    assert any("--hash=sha256:" in line for line in lock["matrx-api"]["lines"])
    assert "matrx-sdk" in lock["matrx-api"]["via"]
