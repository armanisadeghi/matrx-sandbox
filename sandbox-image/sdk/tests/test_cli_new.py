"""Unit half of the `mtx new` guard.

The real proof is `scripts/test-toolchain.sh`, which runs `uv run pytest` on a
freshly scaffolded project inside a real image. This file covers the parts that
do not need a container: the flat layout, the exact next-command output, and
the refusal-with-remedy when the toolchain binary is missing (the failure mode
that made this command necessary in the first place).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from matrx_agent.cli.new import run as new_run


def _args(kind: str, name: str) -> argparse.Namespace:
    return argparse.Namespace(kind=kind, name=name)


@pytest.fixture()
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setenv("MATRX_PROJECTS_ROOT", str(root))
    return root


def test_python_scaffold_is_flat_and_its_test_passes(projects_root, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n, **kw: "/usr/local/bin/uv")
    assert new_run(_args("python", "demo")) == 0

    project = projects_root / "demo"
    # Flat: module and test sit at the project root, no src/ layer.
    assert (project / "pyproject.toml").is_file()
    assert (project / "demo.py").is_file()
    assert (project / "test_demo.py").is_file()
    assert not (project / "src").exists()

    # No build backend to fight with.
    pyproject = (project / "pyproject.toml").read_text()
    assert "[tool.uv]\npackage = false" in pyproject
    assert "build-system" not in pyproject

    # The generated test really passes with the plain interpreter — no venv, no
    # install step. If the scaffold ever emits a broken module or test, this
    # fails.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(project)],
        capture_output=True,
        text=True,
        cwd=str(project),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_python_scaffold_prints_the_next_command(projects_root, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda _n, **kw: "/usr/local/bin/uv")
    assert new_run(_args("python", "demo")) == 0
    out = capsys.readouterr().out
    assert "uv run pytest" in out
    assert str(projects_root / "demo") in out


def test_node_scaffold_is_flat_and_valid_json(projects_root, monkeypatch):
    import json

    monkeypatch.setattr("shutil.which", lambda _n, **kw: "/usr/local/bin/pnpm")
    assert new_run(_args("node", "web-demo")) == 0

    project = projects_root / "web-demo"
    assert (project / "index.js").is_file()
    # '-' is not legal in a module name, so the test file uses the underscored form.
    assert (project / "web_demo.test.js").is_file()
    pkg = json.loads((project / "package.json").read_text())
    assert pkg["name"] == "web-demo"
    assert pkg["scripts"]["test"] == "vitest run"


def test_missing_uv_is_installed_then_the_scaffold_proceeds(
    projects_root, monkeypatch, capsys
):
    """THE GUARD for row ca931876.

    A box created from an older image has no `uv`. Before 2026-09-14 `mtx new`
    refused, the Sandbox Specialist's one sanctioned recipe died on command one,
    and it improvised eight failing shell calls. `mtx new` must now repair the
    box itself and carry on.
    """
    from matrx_agent.cli import toolchain

    state = {"uv": None, "calls": []}
    monkeypatch.setattr(
        "shutil.which", lambda name, **kw: state.get(name, "/usr/local/bin/" + name)
    )

    def fake_ensure(tools=toolchain.REQUIRED_TOOLS, quiet=False):
        state["calls"].append(list(tools))
        for t in tools:
            state[t] = f"/usr/local/bin/{t}"  # the installer really landed it
        return 0

    monkeypatch.setattr(toolchain, "ensure", fake_ensure)

    assert new_run(_args("python", "demo")) == 0
    assert state["calls"] == [["uv"]], "mtx new must ensure the toolchain first"
    project = projects_root / "demo"
    assert (project / "pyproject.toml").is_file()
    assert "uv run pytest" in capsys.readouterr().out


def test_uv_install_failure_still_refuses_loudly_and_names_the_remedy(
    projects_root, monkeypatch, capsys
):
    from matrx_agent.cli import toolchain

    monkeypatch.setattr("shutil.which", lambda _n, **kw: None)
    monkeypatch.setattr(toolchain, "ensure", lambda tools=None, quiet=False: 1)

    assert new_run(_args("python", "demo")) == 1
    err = capsys.readouterr().err
    assert "uv is not on PATH" in err
    assert "mtx toolchain ensure" in err
    assert "astral.sh/uv" in err
    # Nothing half-written: a project that cannot be run is never created.
    assert not (projects_root / "demo").exists()


def test_pnpm_install_failure_still_refuses_loudly(projects_root, monkeypatch, capsys):
    from matrx_agent.cli import toolchain

    monkeypatch.setattr("shutil.which", lambda _n, **kw: None)
    monkeypatch.setattr(toolchain, "ensure", lambda tools=None, quiet=False: 1)

    assert new_run(_args("node", "demo")) == 1
    err = capsys.readouterr().err
    assert "pnpm is not on PATH" in err
    assert not (projects_root / "demo").exists()


def test_rejects_unsafe_names(projects_root, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda _n, **kw: "/usr/local/bin/uv")
    for bad in ["../escape", "Demo Project", "9lives", ""]:
        assert new_run(_args("python", bad)) == 1
    assert "not a usable project name" in capsys.readouterr().err


def test_refuses_to_clobber_a_non_empty_directory(projects_root, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda _n, **kw: "/usr/local/bin/uv")
    existing = projects_root / "demo"
    existing.mkdir(parents=True)
    (existing / "important.py").write_text("# the user's work\n")

    assert new_run(_args("python", "demo")) == 1
    assert "already exists" in capsys.readouterr().err
    assert (existing / "important.py").read_text() == "# the user's work\n"


def test_mtx_new_is_reachable_through_the_cli_dispatcher(projects_root, monkeypatch, capsys):
    """`mtx new ...` must actually route here — a module nothing calls is not a fix."""
    from matrx_agent.cli.__main__ import main

    monkeypatch.setattr("shutil.which", lambda _n, **kw: "/usr/local/bin/uv")
    assert main(["new", "python", "routed"]) == 0
    assert (projects_root / "routed" / "pyproject.toml").is_file()
    assert "uv run pytest" in capsys.readouterr().out


def test_projects_root_defaults_to_home_projects(monkeypatch, tmp_path):
    from matrx_agent.cli.new import _projects_root

    monkeypatch.delenv("MATRX_PROJECTS_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _projects_root() == Path(str(tmp_path)) / "projects"
