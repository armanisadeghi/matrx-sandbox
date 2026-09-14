"""``mtx new`` — scaffold a runnable project in one command.

Why this exists (2026-09-14 agent-efficiency-loop ruling): agents working in a
sandbox burned three to four failed tool calls per Python setup improvising
``uv init`` flags, virtualenv layouts and pytest wiring. The fix is a platform
primitive, not prompt text — one command that lands a flat project with one
passing test and prints the exact next commands.

    mtx new python <name>   →  ~/projects/<name>  (pyproject + module + test)
    mtx new node <name>     →  ~/projects/<name>  (package.json + module + test)

Deliberate design choices:

* **Flat, not ``src/``.** ``uv init --lib`` produces ``src/<pkg>/`` plus a build
  backend; agents then fight editable installs. A flat module beside its test is
  what a one-file experiment actually wants.
* **No build backend.** ``[tool.uv] package = false`` means ``uv run`` only has
  to create a venv — no wheel build, no ``-e .``.
* **No install at creation time.** Creation is offline and instant; the printed
  next command (``uv run pytest`` / ``pnpm install``) does the network work, so
  a failure is attributable to the right step.
* **Missing toolchain repairs itself.** ``uv``/``pnpm`` absent used to be a
  refusal — which on a box created from an older image made the ONE sanctioned
  recipe unusable and pushed the agent straight into the improvisation its
  prompt forbids (independent review, 2026-09-14, row ``ca931876``: eight shell
  calls, eight failures). ``mtx new`` now calls ``mtx toolchain ensure`` first,
  so it works on any box without a migration. It still refuses loudly — naming
  the remedy — when the install itself cannot be done.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

# Name that is safe as a Python module, a directory and an npm package name.
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _projects_root() -> Path:
    return Path(os.environ.get("MATRX_PROJECTS_ROOT") or (Path.home() / "projects"))


def _fail(msg: str) -> int:
    print(f"[mtx new] {msg}", file=sys.stderr)
    return 1


def _validate(name: str) -> str | None:
    if not _SAFE_NAME.match(name):
        return (
            f"'{name}' is not a usable project name. Use lowercase letters, "
            "digits, '-' or '_', starting with a letter (e.g. 'scraper', "
            "'invoice-parser')."
        )
    return None


def _prepare_dir(name: str) -> tuple[Path | None, int]:
    root = _projects_root()
    target = root / name
    if target.exists() and any(target.iterdir()):
        return None, _fail(
            f"{target} already exists and is not empty. Pick another name, or "
            f"work in it directly: cd {target}"
        )
    target.mkdir(parents=True, exist_ok=True)
    return target, 0


def _write(target: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _module_name(name: str) -> str:
    return name.replace("-", "_")


def _ensure_tool(binary: str) -> str | None:
    """Make ``binary`` runnable, installing it if this box's image predates it.

    Returns None on success, or the refusal message when the install failed.
    """
    if shutil.which(binary) is not None:
        return None
    from matrx_agent.cli.toolchain import ensure as toolchain_ensure, _MANUAL

    toolchain_ensure([binary])
    if shutil.which(binary) is not None:
        return None
    return (
        f"{binary} is not on PATH in this sandbox and `mtx toolchain ensure` "
        f"could not install it (see the errors above), so the project was not "
        f"created. Install it by hand with: {_MANUAL[binary]}"
    )


def _new_python(name: str) -> int:
    problem = _ensure_tool("uv")
    if problem:
        return _fail(problem)
    target, rc = _prepare_dir(name)
    if target is None:
        return rc
    mod = _module_name(name)
    _write(
        target,
        {
            "pyproject.toml": (
                "[project]\n"
                f'name = "{name}"\n'
                'version = "0.1.0"\n'
                f'description = "Created by `mtx new python {name}`."\n'
                'requires-python = ">=3.11"\n'
                "dependencies = []\n"
                "\n"
                "[dependency-groups]\n"
                'dev = ["pytest>=8"]\n'
                "\n"
                "# Flat script-style project: nothing to build, so uv only has to\n"
                "# create the venv. Flip to true (and add a build backend) the day\n"
                "# this becomes an installable package.\n"
                "[tool.uv]\n"
                "package = false\n"
            ),
            f"{mod}.py": (
                f'"""{name} — created by `mtx new python {name}`."""\n'
                "\n"
                "\n"
                'def greet(who: str = "world") -> str:\n'
                '    return f"Hello, {who}!"\n'
            ),
            f"test_{mod}.py": (
                f"from {mod} import greet\n"
                "\n"
                "\n"
                "def test_greet() -> None:\n"
                '    assert greet("sandbox") == "Hello, sandbox!"\n'
            ),
            ".gitignore": ".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n",
        },
    )
    print(f"[mtx new] created {target}")
    print("[mtx new] next:")
    print(f"  cd {target} && uv run pytest")
    print("  uv add <package>        # add a dependency")
    print(f"  uv run python {mod}.py  # run it")
    return 0


def _new_node(name: str) -> int:
    problem = _ensure_tool("pnpm")
    if problem:
        return _fail(problem)
    target, rc = _prepare_dir(name)
    if target is None:
        return rc
    mod = _module_name(name)
    _write(
        target,
        {
            "package.json": (
                "{\n"
                f'  "name": "{name}",\n'
                '  "version": "0.1.0",\n'
                '  "private": true,\n'
                '  "type": "module",\n'
                f'  "description": "Created by `mtx new node {name}`.",\n'
                '  "scripts": {\n'
                '    "test": "vitest run"\n'
                "  },\n"
                '  "devDependencies": {\n'
                '    "vitest": "^3.2.4"\n'
                "  }\n"
                "}\n"
            ),
            "index.js": (
                f"// {name} — created by `mtx new node {name}`.\n"
                "export function greet(who = 'world') {\n"
                "  return `Hello, ${who}!`;\n"
                "}\n"
            ),
            f"{mod}.test.js": (
                "import { expect, test } from 'vitest';\n"
                "import { greet } from './index.js';\n"
                "\n"
                "test('greet', () => {\n"
                "  expect(greet('sandbox')).toBe('Hello, sandbox!');\n"
                "});\n"
            ),
            ".gitignore": "node_modules/\n",
        },
    )
    print(f"[mtx new] created {target}")
    print("[mtx new] next:")
    print(f"  cd {target} && pnpm install && pnpm test")
    print("  pnpm add <package>      # add a dependency")
    print("  node index.js           # run it")
    return 0


def run(args) -> int:
    """Entry point wired from ``matrx_agent.cli.__main__``."""
    name: str = args.name
    problem = _validate(name)
    if problem:
        return _fail(problem)
    if args.kind == "python":
        return _new_python(name)
    if args.kind == "node":
        return _new_node(name)
    return _fail(f"unknown project kind '{args.kind}' (expected python or node)")
