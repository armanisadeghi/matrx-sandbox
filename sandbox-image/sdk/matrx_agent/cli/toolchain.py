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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from matrx_agent.cli import errors


class NoWritableToolTarget(RuntimeError):
    """No directory on this box can receive a toolchain binary — already named."""

# ─── Pins. Keep in lockstep with sandbox-image/Dockerfile's ARGs. ────────────
# tests/test_cli_toolchain.py parses the Dockerfile and fails when they drift.
UV_VERSION = "0.10.8"
PNPM_VERSION = "10.15.0"
GH_VERSION = "2.100.0"
NODE_MAJOR = "22"
#: Exact Node used by the self-service path. The image installs the nodesource
#: ``setup_22.x`` stream, so the minor can differ; the FLOOR is what matters and
#: both sides are held to ``NODE_MAJOR``.
NODE_VERSION = "22.23.2"

#: THE TOOLCHAIN CONTRACT (2026-09-18 field report). Every image variant and
#: every self-service upgrade owes an agent the same floor:
#:
#:   * Node >= 22 — Stagehand v4 and friends need it (P1-1: Node 20 gave
#:     ``ReferenceError: WebSocket is not defined``).
#:   * ``npm i -g`` works AS THE AGENT — an image-owned, agent-writable global
#:     prefix (P1-2: EACCES on root-owned /usr/lib/node_modules).
#:   * install scripts run for the agent's own installs, with the policy stated
#:     (P1-3: npm 11's ``allow-scripts`` allowlist silently skips postinstall).
#:   * ``python3`` is a FINAL release >= 3.12 (P2-1: jammy's python3.11 package
#:     is 3.11.0rc1, a 2022 release candidate).
#:   * ``browse`` is on PATH (P2-2).
#:
#: The guard that proves it on a real box is
#: ``sandbox-image/scripts/test-toolchain.sh``.
PYTHON_MIN = (3, 12)
NPM_GLOBAL_PREFIX = "/opt/npm-global"
NPM_POLICY_LINES = (
    "# Matrx sandbox npm policy — see sandbox-image/ADDING_UTILITIES.md.",
    "dangerously-allow-all-scripts=true",
    "ignore-scripts=false",
    "fund=false",
)

#: Every binary the agent-facing setup recipe assumes exists.
REQUIRED_TOOLS = ("uv", "pnpm", "gh", "node")

#: Minimum major version for tools where "present" is not good enough. A tool
#: below its floor is treated exactly like a missing one by ``ensure``.
MIN_MAJOR = {"node": int(NODE_MAJOR)}

#: ``/usr/local/bin`` shims the image installs. ``ensure`` re-creates them on a
#: box that predates them, so a boxload of agents does not have to learn which
#: vintage it is sitting in.
SHIMS = {
    "mtx": '#!/bin/sh\nexec /usr/bin/python3 -m matrx_agent.cli "$@"\n',
    "browse": '#!/bin/sh\nexec /usr/bin/python3 -m matrx_agent.cli browse "$@"\n',
}

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
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A root-owned ~/.local (the 2026-09-15 home-ownership regression) used
        # to end here as an unhandled PermissionError traceback out of `mtx
        # toolchain ensure` and therefore out of `mtx new`. Name it instead.
        raise NoWritableToolTarget(
            errors.permission_message(
                exc, action=f"creating the tool directory {fallback}"
            )
        ) from exc
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


def _install_node(target: _Target) -> tuple[bool, str]:
    """Put Node >= 22 on an OLD box, without root and without a migration.

    Existing boxes are never force-migrated (SBX-006), so an image that ships
    Node 22 does nothing for a box created last month — and that box is where
    the agent that hit ``ReferenceError: WebSocket is not defined`` actually
    lives. The official linux tarball is one download, works as a non-root
    user, and brings its own npm/npx, which is why it beats nodesource+apt
    here for the same reason ``gh`` comes from a tarball.

    The tree lands in ``<target>/../lib/matrx-node-<major>`` (or ``~/.local``
    when we do not own the prefix) and only the three entry points are linked
    onto PATH, ahead of whatever old Node the box still carries.
    """
    arch = _arch()
    if arch is None:
        return False, f"unsupported architecture {platform.machine()}"
    stem = f"node-v{NODE_VERSION}-linux-{'x64' if arch == 'amd64' else 'arm64'}"
    url = f"https://nodejs.org/dist/v{NODE_VERSION}/{stem}.tar.xz"
    dest_root = target.directory.parent / "lib" / f"matrx-node-{NODE_MAJOR}"
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "node.tar.xz"
        dl = subprocess.run(
            ["curl", "-fsSL", url, "-o", str(tarball)], capture_output=True, text=True
        )
        if dl.returncode != 0:
            return False, f"could not download {url}: {dl.stderr.strip()[:200]}"
        ex = subprocess.run(
            ["tar", "-xJf", str(tarball), "-C", tmp], capture_output=True, text=True
        )
        if ex.returncode != 0:
            return False, f"could not unpack the node tarball: {ex.stderr.strip()[:200]}"
        extracted = Path(tmp) / stem
        if not (extracted / "bin" / "node").exists():
            return False, f"node binary missing from tarball (expected {extracted}/bin/node)"
        mk = target.run(["mkdir", "-p", str(dest_root.parent)], capture_output=True, text=True)
        if mk.returncode != 0:
            return False, (mk.stderr or mk.stdout).strip()[:300]
        target.run(["rm", "-rf", str(dest_root)], capture_output=True, text=True)
        mv = target.run(
            ["cp", "-a", str(extracted), str(dest_root)], capture_output=True, text=True
        )
        if mv.returncode != 0:
            return False, (mv.stderr or mv.stdout).strip()[:300]
    for name in ("node", "npm", "npx"):
        link = target.run(
            ["ln", "-sf", str(dest_root / "bin" / name), str(target.directory / name)],
            capture_output=True,
            text=True,
        )
        if link.returncode != 0:
            return False, f"installed at {dest_root} but could not link {name} into {target.directory}"
    return True, f"node {NODE_VERSION} -> {target.directory}/node (tree at {dest_root})"


def _npm_prefix_target(target: _Target) -> tuple[bool, str]:
    """Make ``npm i -g`` work as the agent, and make install scripts run.

    On a current image this is already true (the Dockerfile owns it). On an old
    box the global prefix is ``/usr`` — root-owned — and npm 11's
    ``allow-scripts`` allowlist is unset, so the agent gets EACCES on one
    command and a silently script-less install on the next. Both are fixed the
    same way the image fixes them: an image-level, agent-owned prefix at
    ``/opt/npm-global``, with the policy written into its own ``etc/npmrc``.

    NOT ``~/.npm-global``: the home is restored wholesale from a volume or S3 at
    boot, so a prefix inside it is wiped or resurrected stale across an image
    swap.
    """
    prefix = Path(NPM_GLOBAL_PREFIX)
    uid, gid = os.getuid(), os.getgid()
    mk = target.run(
        ["mkdir", "-p", str(prefix / "bin"), str(prefix / "lib"), str(prefix / "etc")],
        capture_output=True,
        text=True,
    )
    if mk.returncode != 0:
        return False, (mk.stderr or mk.stdout).strip()[:300]
    ch = target.run(
        ["chown", "-R", f"{uid}:{gid}", str(prefix)], capture_output=True, text=True
    )
    if ch.returncode != 0 and not os.access(prefix / "lib", os.W_OK):
        return False, f"{prefix} is not writable by uid {uid}: {(ch.stderr or ch.stdout).strip()[:200]}"
    try:
        (prefix / "etc" / "npmrc").write_text("\n".join(NPM_POLICY_LINES) + "\n")
    except OSError as exc:
        return False, f"could not write {prefix}/etc/npmrc: {exc}"
    return True, f"npm global prefix {prefix} (agent-owned, install scripts allowed)"


def ensure_npm_policy(quiet: bool = False) -> bool:
    """Apply the npm half of the contract to THIS box. Idempotent."""
    prefix = Path(NPM_GLOBAL_PREFIX)
    npmrc = prefix / "etc" / "npmrc"
    already = (
        os.access(prefix / "lib", os.W_OK)
        and npmrc.exists()
        and "dangerously-allow-all-scripts=true" in npmrc.read_text()
    )
    if not already:
        try:
            target = _pick_target()
        except NoWritableToolTarget as exc:
            _warn(str(exc))
            return False
        ok, detail = _npm_prefix_target(target)
        if not ok:
            _warn(
                f"could not make `npm i -g` work as this user: {detail}\n"
                f"  do it by hand with: sudo install -d -o $(id -u) -g $(id -g) "
                f"{prefix}/bin {prefix}/lib {prefix}/etc && "
                f"printf 'dangerously-allow-all-scripts=true\\n' > {prefix}/etc/npmrc"
            )
            return False
        if not quiet:
            _say(f"configured {detail}")
    # The env half: this process's children, plus every future shell. sshd
    # starts shells with none of the container env, same reason the image also
    # writes /etc/profile.d.
    bin_dir = prefix / "bin"
    if os.environ.get("NPM_CONFIG_PREFIX") == str(prefix) and str(bin_dir) in _path_entries():
        # A current image already publishes both (ENV + /etc/profile.d). Leave
        # the user's shell profiles alone.
        return True
    os.environ["NPM_CONFIG_PREFIX"] = str(prefix)
    if str(bin_dir) not in _path_entries():
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        _persist_path(bin_dir)
    _persist_npm_prefix(prefix)
    return True


def _persist_npm_prefix(prefix: Path) -> None:
    """Write NPM_CONFIG_PREFIX into the shell profiles, once."""
    marker = "# npm prefix added by `mtx toolchain ensure`"
    line = f'export NPM_CONFIG_PREFIX="{prefix}"'
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


def ensure_shims(quiet: bool = False) -> None:
    """Create ``mtx`` / ``browse`` in /usr/local/bin on a box that predates them.

    Only ever CREATE — a shim that already exists is a command that works on
    this box today, and rewriting it is how you break the thing you came to fix.
    """
    for name, body in SHIMS.items():
        path = Path(_LOCAL_BIN) / name
        if path.exists():
            continue
        try:
            target = _pick_target()
        except NoWritableToolTarget as exc:
            _warn(str(exc))
            return
        proc = target.run(
            ["sh", "-c", f"printf '%s' {shlex.quote(body)} > {shlex.quote(str(path))} "
                         f"&& chmod 0755 {shlex.quote(str(path))}"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            if not quiet:
                _say(f"installed the `{name}` shim at {path}")
        else:
            _warn(
                f"could not install the `{name}` shim at {path}: "
                f"{(proc.stderr or proc.stdout).strip()[:200]}\n"
                f"  run it directly instead: python3 -m matrx_agent.cli "
                f"{'browse ' if name == 'browse' else ''}--help"
            )


def _major(output: str) -> int | None:
    match = re.search(r"(\d+)", output.strip().lstrip("v"))
    return int(match.group(1)) if match else None


def tool_version(name: str) -> str:
    try:
        proc = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable ({exc})"
    return (proc.stdout or proc.stderr).strip().splitlines()[0] if proc.returncode == 0 else "unavailable"


def _below_floor(name: str) -> bool:
    """True when ``name`` is present but older than the contract allows."""
    floor = MIN_MAJOR.get(name)
    if floor is None or shutil.which(name) is None:
        return False
    major = _major(tool_version(name))
    return major is not None and major < floor


_INSTALLERS = {"uv": _install_uv, "pnpm": _install_pnpm, "gh": _install_gh, "node": _install_node}

_MANUAL = {
    "uv": f"curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh | sh",
    "pnpm": f"npm install -g pnpm@{PNPM_VERSION}",
    "gh": (
        f"curl -fsSL https://github.com/cli/cli/releases/download/v{GH_VERSION}/"
        f"gh_{GH_VERSION}_linux_amd64.tar.gz | tar -xz"
    ),
    "node": (
        f"curl -fsSL https://nodejs.org/dist/v{NODE_VERSION}/"
        f"node-v{NODE_VERSION}-linux-x64.tar.xz | tar -xJ"
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

    # The npm prefix + install-script policy and the /usr/local/bin shims are
    # part of the same contract as the binaries, and they are what an OLD box
    # is missing even when every binary is present. Always reconcile them.
    npm_ok = ensure_npm_policy(quiet=quiet)
    ensure_shims(quiet=quiet)

    # "Present" is not the bar for every tool: an old box has node 20, which is
    # exactly the defect (P1-1). A tool below its floor is installed over.
    missing = [
        name for name in wanted if shutil.which(name) is None or _below_floor(name)
    ]
    if not missing:
        if not quiet:
            _say(
                "already present: "
                + ", ".join(f"{n} ({shutil.which(n)})" for n in wanted)
                + " — nothing to do."
            )
        return 1 if (unknown or not npm_ok) else 0

    try:
        target = _pick_target()
    except NoWritableToolTarget as exc:
        _warn(str(exc))
        _warn(
            "no writable directory for "
            + ", ".join(missing)
            + f"; install by hand with: {'; '.join(_MANUAL[n] for n in missing)}"
        )
        return 1
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
    return 0 if not failures and not unknown and npm_ok else 1


def run(args) -> int:
    """Entry point wired from ``matrx_agent.cli.__main__``."""
    action = getattr(args, "toolchain_cmd", "ensure")
    tools = getattr(args, "tools", None) or list(REQUIRED_TOOLS)
    if action == "check":
        broken: list[str] = []
        for name in tools:
            where = shutil.which(name)
            if where is None:
                print(f"{name}: MISSING")
                broken.append(name)
                continue
            version = tool_version(name)
            floor = MIN_MAJOR.get(name)
            if _below_floor(name):
                print(f"{name}: {where} ({version}) — BELOW THE FLOOR (needs >= {floor})")
                broken.append(name)
            else:
                print(f"{name}: {where} ({version})")

        # The rest of the toolchain contract — the parts that are not binaries.
        for shim in SHIMS:
            path = Path(_LOCAL_BIN) / shim
            print(f"{shim} shim: {path if path.exists() else 'MISSING'}")
            if not path.exists():
                broken.append(shim)

        prefix = Path(NPM_GLOBAL_PREFIX)
        npmrc = prefix / "etc" / "npmrc"
        writable = os.access(prefix / "lib", os.W_OK)
        policy = npmrc.exists() and "dangerously-allow-all-scripts=true" in npmrc.read_text()
        print(
            f"npm global prefix: {prefix} "
            f"({'writable' if writable else 'NOT WRITABLE BY THIS USER'}, "
            f"{'install scripts allowed' if policy else 'NO SCRIPT POLICY'})"
        )
        if not (writable and policy):
            broken.append("npm-global-prefix")

        version = sys.version_info
        release = version[:2] >= PYTHON_MIN and version.releaselevel == "final"
        print(
            f"python3: {sys.executable} ({sys.version.split()[0]})"
            + ("" if release else f" — NOT A RELEASE >= {'.'.join(map(str, PYTHON_MIN))}")
        )
        if not release:
            # Never silently pass: an old box's python3 is 3.11.0rc1 and only a
            # new image fixes it (this command cannot repoint an interpreter the
            # SDK is installed into). Say so with the remedy.
            _warn(
                "this box's python3 is not a release >= "
                f"{'.'.join(map(str, PYTHON_MIN))}. `mtx toolchain ensure` cannot "
                "change it — the SDK is installed into that interpreter. Recreate "
                "the box on the current image to get it."
            )
            broken.append("python3")

        if broken:
            _warn(
                f"not to contract: {', '.join(broken)} — fix what is fixable here with: "
                "mtx toolchain ensure"
            )
            return 1
        return 0
    return ensure(tools)
