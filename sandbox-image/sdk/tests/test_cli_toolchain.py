"""Guards for `mtx toolchain ensure` — the self-service upgrade for old boxes.

The class this closes (independent review 2026-09-14, review row ca931876):
shipping uv/pnpm/gh in the image only helps boxes created after the build, and
existing user boxes are never force-migrated (SBX-006). So the image alone can
never make the agent's mandated recipe work. `mtx toolchain ensure` has to work
on a box exactly as it stands.

These are the parts provable without a container. The real proof is
`scripts/test-toolchain.sh` in an image, plus the live old-box smoke recorded on
the review row.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from matrx_agent.cli import toolchain
from matrx_agent.cli.__main__ import main

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


# ─── The pins the image and the self-service path share ─────────────────────


@pytest.mark.parametrize(
    ("arg_name", "constant"),
    [
        ("UV_VERSION", toolchain.UV_VERSION),
        ("PNPM_VERSION", toolchain.PNPM_VERSION),
    ],
)
def test_dockerfile_pins_match_the_module(arg_name, constant):
    """A box upgraded by hand must get the same toolchain the image ships.

    If these drift, a `mtx toolchain ensure` box and a fresh box run different
    uv/pnpm — the exact "prompt says X, box does Y" class this work exists to kill.
    """
    text = DOCKERFILE.read_text()
    found = re.search(rf"^ARG {arg_name}=(\S+)", text, re.M)
    assert found, f"{arg_name} ARG missing from {DOCKERFILE}"
    assert found.group(1) == constant, (
        f"Dockerfile pins {arg_name}={found.group(1)} but "
        f"matrx_agent/cli/toolchain.py pins {constant}. Bump both together."
    )


def test_every_required_tool_is_installed_by_the_dockerfile():
    """The prompt-mandated binaries must be in the image, not only installable.

    `mtx toolchain ensure` is the repair for old boxes; it is not a licence for
    the image to stop shipping them.
    """
    text = DOCKERFILE.read_text()
    for binary in toolchain.REQUIRED_TOOLS:
        assert re.search(rf"^\s*&&\s*{binary} --version", text, re.M), (
            f"Dockerfile never verifies `{binary} --version`; the image can ship "
            f"without a binary the agent prompt declares mandatory."
        )


def test_every_required_tool_has_an_installer_and_a_manual_command():
    for binary in toolchain.REQUIRED_TOOLS:
        assert binary in toolchain._INSTALLERS
        assert binary in toolchain._MANUAL


# ─── ensure() behaviour ─────────────────────────────────────────────────────


def test_ensure_is_a_no_op_when_everything_is_present(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda n, **kw: f"/usr/local/bin/{n}")
    monkeypatch.setattr(
        toolchain, "_INSTALLERS", {n: _never_called for n in toolchain.REQUIRED_TOOLS}
    )
    assert toolchain.ensure() == 0
    assert "nothing to do" in capsys.readouterr().out


def _never_called(_target):  # pragma: no cover - reaching this IS the failure
    raise AssertionError("installer ran for a tool that was already present")


def test_ensure_installs_only_what_is_missing(monkeypatch, capsys):
    present = {"pnpm", "gh"}
    installed: list[str] = []

    def which(name, path=None, **kw):
        if name in present:
            return f"/usr/local/bin/{name}"
        return None

    monkeypatch.setattr("shutil.which", which)
    monkeypatch.setattr(
        toolchain,
        "_pick_target",
        lambda: toolchain._Target(Path("/usr/local/bin"), [], True),
    )

    def fake_uv(target):
        installed.append("uv")
        present.add("uv")
        return True, "uv 0.0.0 -> /usr/local/bin/uv"

    monkeypatch.setattr(toolchain, "_INSTALLERS", {**toolchain._INSTALLERS, "uv": fake_uv})

    assert toolchain.ensure() == 0
    assert installed == ["uv"]
    out = capsys.readouterr().out
    assert "missing: uv" in out
    assert "installed uv" in out


def test_ensure_reports_failure_with_the_manual_command(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda n, **kw: None)
    monkeypatch.setattr(
        toolchain,
        "_pick_target",
        lambda: toolchain._Target(Path("/usr/local/bin"), [], True),
    )
    monkeypatch.setattr(
        toolchain,
        "_INSTALLERS",
        {"uv": lambda t: (False, "network is unreachable")},
    )
    assert toolchain.ensure(["uv"]) == 1
    err = capsys.readouterr().err
    assert "could not install uv" in err
    assert "network is unreachable" in err
    assert "astral.sh/uv" in err  # the manual command, never a bare failure


def test_ensure_never_reports_success_for_an_installer_that_lied(monkeypatch, capsys):
    """An installer returning ok=True without a runnable binary is still a failure."""
    monkeypatch.setattr("shutil.which", lambda n, **kw: None)
    monkeypatch.setattr(
        toolchain,
        "_pick_target",
        lambda: toolchain._Target(Path("/usr/local/bin"), [], True),
    )
    monkeypatch.setattr(toolchain, "_INSTALLERS", {"uv": lambda t: (True, "definitely done")})
    assert toolchain.ensure(["uv"]) == 1
    assert "could not install uv" in capsys.readouterr().err


# ─── target selection: install where PATH already looks ─────────────────────


def test_target_prefers_usr_local_bin_when_writable(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setattr("os.access", lambda p, mode: True)
    target = toolchain._pick_target()
    assert target.directory == Path("/usr/local/bin")
    assert target.prefix == []
    assert target.on_path


def test_target_borrows_sudo_when_usr_local_bin_is_root_owned(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setattr("os.access", lambda p, mode: False)
    monkeypatch.setattr(toolchain, "_have_sudo", lambda: True)
    target = toolchain._pick_target()
    assert target.directory == Path("/usr/local/bin")
    assert target.prefix == ["sudo", "-n"]


def test_target_falls_back_to_local_bin_without_sudo(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("os.access", lambda p, mode: False)
    monkeypatch.setattr(toolchain, "_have_sudo", lambda: False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = toolchain._pick_target()
    assert target.directory == tmp_path / ".local" / "bin"
    assert target.directory.is_dir()
    assert not target.on_path


def test_path_is_persisted_and_announced_when_the_dir_is_not_on_path(
    monkeypatch, tmp_path, capsys
):
    """A non-interactive shell_execute shell sources no profile — say the line."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    bindir = tmp_path / ".local" / "bin"
    toolchain._persist_path(bindir)
    for name in (".bashrc", ".profile"):
        assert f'export PATH="{bindir}:$PATH"' in (tmp_path / name).read_text()
    assert f'export PATH="{bindir}:$PATH"' in capsys.readouterr().err

    # Idempotent: a second run must not duplicate the line.
    toolchain._persist_path(bindir)
    assert (tmp_path / ".bashrc").read_text().count("export PATH=") == 1


# ─── the CLI really routes here ─────────────────────────────────────────────


def test_mtx_toolchain_ensure_is_reachable_through_the_dispatcher(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        toolchain, "ensure", lambda tools=None, quiet=False: seen.setdefault("tools", tools) and 0
    )
    monkeypatch.setattr("shutil.which", lambda n, **kw: f"/usr/local/bin/{n}")
    assert main(["toolchain", "ensure"]) == 0
    assert seen["tools"] == list(toolchain.REQUIRED_TOOLS)


def test_mtx_toolchain_check_names_what_is_missing(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda n, **kw: None if n == "gh" else f"/bin/{n}")
    assert main(["toolchain", "check"]) == 1
    captured = capsys.readouterr()
    assert "gh: MISSING" in captured.out
    assert "mtx toolchain ensure" in captured.err


def test_unwritable_local_dir_is_named_not_a_traceback(tmp_path, monkeypatch, capsys):
    """`mtx toolchain ensure` on a box whose ~/.local is root-owned.

    The 2026-09-15 regression reached `Path.home()/".local"/"bin".mkdir()` with
    a root-owned ~/.local and raised PermissionError straight out of `mtx new`.
    Fails against the pre-fix code: the exception escapes `ensure()`.
    """
    from matrx_agent.cli import toolchain

    home = tmp_path / "home"
    (home / ".local").mkdir(parents=True)
    (home / ".local").chmod(0o555)
    monkeypatch.setattr(toolchain.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(toolchain.shutil, "which", lambda _n, **kw: None)
    monkeypatch.setattr(toolchain, "_have_sudo", lambda: False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    try:
        rc = toolchain.ensure(["uv"])
    finally:
        (home / ".local").chmod(0o755)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "permission denied" in err
    assert "install by hand" in err
