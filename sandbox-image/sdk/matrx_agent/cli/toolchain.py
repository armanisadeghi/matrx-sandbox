"""``mtx toolchain ensure`` — make the agent project toolchain present on ANY box.

Why this exists (2026-09-14 independent review of the Sandbox Specialist agent,
review row ``ca931876``): the agent's prompt mandates ``mtx new python <name>``
and forbids improvising. On a box created from an older image ``uv`` was not on
PATH, the sanctioned recipe died on command one, and the agent fell through to
exactly the improvisation the prompt forbids — eight shell calls, eight
failures, no project.

Shipping the toolchain in the image only fixes boxes created *after* the build.
Existing user boxes are never force-migrated (SBX-006), so the image alone can
never close this class. The close is a **self-service, one-command upgrade that
works on a box as it stands**::

    mtx toolchain ensure

It installs whatever is missing (``uv``, ``pnpm``, ``gh``) into a directory that
is already on the agent's PATH, is idempotent (a second run is a no-op that says
so), prints exactly what it did, and is called automatically by ``mtx new``
before it scaffolds anything.

Design choices worth keeping:

* **Pinned versions, never ``@latest``.** Same reason the Dockerfile pins them:
  one unpinned ``npm@latest`` broke every image build on both pipelines for a
  day (2026-07-08). ``tests/test_cli_toolchain.py`` fails if these constants and
  the Dockerfile ARGs ever disagree, so the image and the self-service path can
  never install different toolchains.
* **Install where PATH already looks.** ``/usr/local/bin`` directly if writable,
  else via passwordless ``sudo`` (every sandbox image grants it), else
  ``~/.local/bin`` — and in that last case we persist the PATH entry to the shell
  profiles *and* say the export line out loud, because a non-interactive
  ``shell_execute`` shell sources neither.
* **``gh`` comes from its pinned release tarball, not apt.** apt needs root, a
  keyring and a repo file; the tarball is one download that also works for a
  non-root agent.
* **Nothing fails silently.** Every install announces itself; every failure names
  the binary, the reason and the exact manual command to run instead.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ─── Pins. Keep in lockstep with sandbox-image/Dockerfile's ARGs. ────────────
# tests/test_cli_toolchain.py parses the Dockerfile and fails when they drift.
UV_VERSION = "0.10.8"
PNPM_VERSION = "10.15.0"
GH_VERSION = "2.100.0"

#: Every binary the agent-facing setup recipe assumes exists.
REQUIRED_TOOLS = ("uv", "pnpm", "gh")

_LOCAL_BIN = "/usr/local/bin"


def _say(msg: str) -> None:
    print(f"[mtx toolchain] {msg}")


def _warn(msg: str) -> None:
    print(f"[mtx toolchain] {msg}", file=sys.stderr)


def _have_sudo() -> bool:
    try:
        return (
            subprocess.run(
                ["sudo", "-n", "true"],
                capture_output=True,
                timeout=15,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


class _Target:
    """Where binaries go, and how to write there.

    ``prefix`` is the argv prefix needed to write into ``directory`` (empty when
    we own it, ``["sudo", "-n"]`` when we borrow root). ``on_path`` says whether
    the directory is already on PATH — when it is not, the caller has to persist
    it and tell the user.
    """

    def __init__(self, directory: Path, prefix: list[str], on_path: bool):
        self.directory = directory
        self.prefix = prefix
        self.on_path = on_path

    def run(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(self.prefix + argv, **kwargs)

    def run_env(self, env: dict[str, str], argv: list[str], **kwargs):
        """Run ``argv`` with extra env vars, surviving the sudo env scrub."""
        if self.prefix:
            wrapped = [*self.prefix, "env", *[f"{k}={v}" for k, v in env.items()], *argv]
            return subprocess.run(wrapped, **kwargs)
        merged = {**os.environ, **env}
        return subprocess.run(argv, env=merged, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Target({self.directory}, sudo={bool(self.prefix)})"


def _path_entries() -> list[str]:
    return [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]


def _pick_target() -> _Target:
    local = Path(_LOCAL_BIN)
    on_path = _LOCAL_BIN in _path_entries()
    if on_path and os.access(local, os.W_OK):
        return _Target(local, [], True)
    if on_path and _have_sudo():
        return _Target(local, ["sudo", "-n"], True)
    fallback = Path.home() / ".local" / "bin"
    fallback.mkdir(parents=True, exist_ok=True)
    return _Target(fallback, [], str(fallback) in _path_entries())


def _persist_path(directory: Path) -> None:
    """Append ``directory`` to the shell profiles, once, and say the line out loud."""
    line = f'export PATH="{directory}:$PATH"'
    marker = "# added by `mtx toolchain ensure`"
    for name in (".bashrc", ".profile"):
        profile = Path.home() / name
        try:
            existing = profile.read_text() if profile.exists() else ""
            if marker in existing:
                continue
            with profile.open("a") as fh:
                fh.write(f"\n{marker}\n{line}\n")
        except OSError as exc:  # pragma: no cover - unwritable home
            _warn(f"could not update {profile}: {exc}")
    _warn(
        f"{directory} is not on this shell's PATH. It was added to ~/.bashrc and "
        "~/.profile for future shells; in THIS shell run:\n"
        f"  {line}"
    )


def _arch() -> str | None:
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine()
    )


# ─── Installers. Each returns (ok, detail). ──────────────────────────────────


def _install_uv(target: _Target) -> tuple[bool, str]:
    url = f"https://astral.sh/uv/{UV_VERSION}/install.sh"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "uv-install.sh"
        dl = subprocess.run(
            ["curl", "-LsSf", url, "-o", str(script)], capture_output=True, text=True
        )
        if dl.returncode != 0:
            return False, f"could not download {url}: {dl.stderr.strip()[:200]}"
        os.chmod(script, 0o755)
        proc = target.run_env(
            {"UV_INSTALL_DIR": str(target.directory), "UV_NO_MODIFY_PATH": "1"},
            ["sh", str(script)],
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:300]
    return True, f"uv {UV_VERSION} -> {target.directory}/uv"


def _install_pnpm(target: _Target) -> tuple[bool, str]:
    # Preferred: the same `npm install -g` the Dockerfile uses, so the image and
    # the self-service path land identical bits.
    if shutil.which("npm"):
        proc = target.run(
            ["npm", "install", "-g", f"pnpm@{PNPM_VERSION}"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and shutil.which("pnpm"):
            return True, f"pnpm {PNPM_VERSION} (npm -g)"
        npm_err = (proc.stderr or proc.stdout).strip()[:200]
    else:
        npm_err = "npm is not on PATH"

    # Fallback: the standalone installer, into the user's own PNPM_HOME, then a
    # link from the directory we know is on PATH.
    home = Path.home() / ".local" / "share" / "pnpm"
    proc = subprocess.run(
        ["sh", "-c", "curl -fsSL https://get.pnpm.io/install.sh | sh -"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PNPM_VERSION": PNPM_VERSION,
            "PNPM_HOME": str(home),
            "SHELL": "/bin/bash",
        },
    )
    binary = home / "pnpm"
    if proc.returncode != 0 or not binary.exists():
        detail = (proc.stderr or proc.stdout).strip()[:200]
        return False, f"npm route failed ({npm_err}); standalone installer failed ({detail})"
    link = target.directory / "pnpm"
    ln = target.run(["ln", "-sf", str(binary), str(link)], capture_output=True, text=True)
    if ln.returncode != 0:
        return False, f"installed at {binary} but could not link into {target.directory}"
    return True, f"pnpm {PNPM_VERSION} (standalone) -> {link}"


def _install_gh(target: _Target) -> tuple[bool, str]:
    arch = _arch()
    if arch is None:
        return False, f"unsupported architecture {platform.machine()}"
    stem = f"gh_{GH_VERSION}_linux_{arch}"
    url = f"https://github.com/cli/cli/releases/download/v{GH_VERSION}/{stem}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "gh.tar.gz"
        dl = subprocess.run(
            ["curl", "-fsSL", url, "-o", str(tarball)], capture_output=True, text=True
        )
        if dl.returncode != 0:
            return False, f"could not download {url}: {dl.stderr.strip()[:200]}"
        ex = subprocess.run(
            ["tar", "-xzf", str(tarball), "-C", tmp], capture_output=True, text=True
        )
        if ex.returncode != 0:
            return False, f"could not unpack gh tarball: {ex.stderr.strip()[:200]}"
        extracted = Path(tmp) / stem / "bin" / "gh"
        if not extracted.exists():
            return False, f"gh binary missing from tarball (expected {extracted})"
        cp = target.run(
            ["install", "-m", "0755", str(extracted), str(target.directory / "gh")],
            capture_output=True,
            text=True,
        )
    if cp.returncode != 0:
        return False, (cp.stderr or cp.stdout).strip()[:300]
    return True, f"gh {GH_VERSION} -> {target.directory}/gh"


_INSTALLERS = {"uv": _install_uv, "pnpm": _install_pnpm, "gh": _install_gh}

_MANUAL = {
    "uv": f"curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh | sh",
    "pnpm": f"npm install -g pnpm@{PNPM_VERSION}",
    "gh": (
        f"curl -fsSL https://github.com/cli/cli/releases/download/v{GH_VERSION}/"
        f"gh_{GH_VERSION}_linux_amd64.tar.gz | tar -xz"
    ),
}


def ensure(tools: tuple[str, ...] | list[str] = REQUIRED_TOOLS, quiet: bool = False) -> int:
    """Install any of ``tools`` that are missing. Idempotent; 0 = all present.

    Returns 0 when every requested tool is on PATH afterwards, 1 otherwise. On
    failure the reason and the manual command are printed — never a silent skip.
    """
    wanted = [t for t in tools if t in _INSTALLERS]
    unknown = [t for t in tools if t not in _INSTALLERS]
    for name in unknown:
        _warn(f"unknown tool '{name}' (known: {', '.join(sorted(_INSTALLERS))})")

    missing = [name for name in wanted if shutil.which(name) is None]
    if not missing:
        if not quiet:
            _say(
                "already present: "
                + ", ".join(f"{n} ({shutil.which(n)})" for n in wanted)
                + " — nothing to do."
            )
        return 1 if unknown else 0

    target = _pick_target()
    if not quiet:
        _say(
            f"missing: {', '.join(missing)} — installing into {target.directory}"
            + (" (via sudo)" if target.prefix else "")
        )

    installed: list[str] = []
    failures: list[str] = []
    for name in missing:
        ok, detail = _INSTALLERS[name](target)
        # Verify against the install directory explicitly: it may not be on this
        # process's PATH yet, and "installed" is only true if the binary is there.
        search = os.pathsep.join([str(target.directory), *_path_entries()])
        if ok and shutil.which(name, path=search):
            installed.append(detail)
            _say(f"installed {detail}")
        else:
            failures.append(name)
            _warn(
                f"could not install {name}: {detail or 'binary still not runnable'}\n"
                f"  install it by hand with: {_MANUAL[name]}"
            )

    if not target.on_path and installed:
        _persist_path(target.directory)

    if not quiet:
        present = [n for n in wanted if n not in failures]
        _say(f"ready: {', '.join(present) if present else '(none)'}")
    return 0 if not failures and not unknown else 1


def run(args) -> int:
    """Entry point wired from ``matrx_agent.cli.__main__``."""
    action = getattr(args, "toolchain_cmd", "ensure")
    tools = getattr(args, "tools", None) or list(REQUIRED_TOOLS)
    if action == "check":
        missing = [n for n in tools if shutil.which(n) is None]
        for name in tools:
            where = shutil.which(name)
            print(f"{name}: {where or 'MISSING'}")
        if missing:
            _warn(
                f"missing: {', '.join(missing)} — fix every one of them with: "
                "mtx toolchain ensure"
            )
            return 1
        return 0
    return ensure(tools)
