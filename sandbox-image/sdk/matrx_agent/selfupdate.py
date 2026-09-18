"""Runtime SDK refresh — install the current SDK tree into a running sandbox.

THE GAP THIS CLOSES. The ``matrx_agent`` SDK (the ``mtx`` CLI, the in-container
daemon) is baked into the image, and an existing box is never force-migrated
(SBX-006). So a box created in August can never run a command the SDK grew in
September — ``mtx toolchain ensure`` was the first casualty, and the workaround
was a three-command shell fallback pasted into a prompt.

THE MECHANISM (one code path, two triggers). The orchestrator stages the CURRENT
image's ``/opt/sandbox/sdk`` tree into the running container at
``/opt/sandbox/sdk.incoming`` (``docker cp`` from an image it already has), then
runs THIS MODULE out of the staged tree — so the installer is always the new
code, never whatever ancient copy the box is carrying. ``mtx self-update`` runs
exactly the same function on demand.

THE RULES.
* ``/home/agent`` is never touched. The refresh writes ``/opt/sandbox/**`` and
  ``/usr/local/bin/mtx`` only, and refuses outright if asked to target a home.
* The swap is a rename, not an overwrite: the new tree lands beside the old one
  and is renamed into place, so a half-copied SDK can never be what the box
  imports. The previous tree is kept for one refresh as ``sdk.prev`` and is the
  rollback.
* The pip editable install baked by the Dockerfile points at the PATH
  ``/opt/sandbox/sdk``, so a rename into that path is the whole install. No pip,
  no network. A release that adds a third-party DEPENDENCY still needs a real
  image migration — this reports ``deps_checked`` so that case is visible
  instead of mysterious.
* The daemon (uvicorn ``matrx_agent.api.main``) holds every PTY session in
  process: pty.fork()'s child shell dies with it. So the daemon is restarted
  ONLY when the daemon's own code actually changed AND the caller passes
  ``--allow-daemon-restart`` (the orchestrator passes it only when no PTY/watch
  attachment is open). Otherwise the restart is DEFERRED and said out loud: the
  CLI half is live immediately either way, because ``mtx`` is a fresh process.
* Every outcome is printed as one JSON object. Nothing here fails silently.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TARGET = "/opt/sandbox/sdk"
DEFAULT_SOURCE = "/opt/sandbox/sdk.incoming"
STAMP_NAME = ".matrx-sdk-refresh"
VERSION_NAME = "VERSION"
MTX_SHIM = "/usr/local/bin/mtx"
# /usr/bin/python3 — the update-alternatives symlink, NOT a hardcoded minor.
# It points at 3.12 in current images and at whatever an older box carries,
# so one shim body is correct everywhere and no image bump can strand it.
MTX_SHIM_BODY = '#!/bin/sh\nexec /usr/bin/python3 -m matrx_agent.cli "$@"\n'
BROWSE_SHIM = "/usr/local/bin/browse"
BROWSE_SHIM_BODY = '#!/bin/sh\nexec /usr/bin/python3 -m matrx_agent.cli browse "$@"\n'
DAEMON_MARKER = "matrx_agent.api.main"
DAEMON_HEALTH = "http://127.0.0.1:8000/health"

# Directories whose contents change the DAEMON's behaviour. A change confined
# outside this set (the CLI, for example) never justifies dropping a PTY.
_DAEMON_DIRS = ("matrx_agent/api", "matrx_agent/cloud_sync", "matrx_agent/persistence")
_SKIP_DIRS = {"__pycache__", ".git", "tests", "node_modules"}


class RefuseToInstall(RuntimeError):
    """The install was asked to do something it must never do."""


# ── digests ──────────────────────────────────────────────────────────────────


def _iter_py(root: str, prefixes: tuple[str, ...] | None = None):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if prefixes and not rel.startswith(prefixes):
                continue
            yield rel, full


def tree_digest(root: str, prefixes: tuple[str, ...] | None = None) -> str:
    """Content digest of a tree's Python sources. Stable across copies: it reads
    bytes and paths only, never mtimes (a ``docker cp`` rewrites those)."""
    if not os.path.isdir(root):
        return ""
    h = hashlib.sha256()
    for rel, full in _iter_py(root, prefixes):
        h.update(rel.replace(os.sep, "/").encode())
        h.update(b"\0")
        try:
            with open(full, "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()


def daemon_digest(root: str) -> str:
    return tree_digest(root, _DAEMON_DIRS)


def read_stamp(target: str = DEFAULT_TARGET) -> dict:
    """What this box was last refreshed to. ``{}`` when it is still wearing the
    SDK its image baked (the normal case for a fresh box)."""
    try:
        with open(os.path.join(target, STAMP_NAME), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def installed_version(target: str = DEFAULT_TARGET) -> str:
    """The SDK version actually on disk: the refresh stamp wins over the baked
    VERSION file, because after a refresh the baked file is the OLD truth."""
    stamped = read_stamp(target).get("to_version")
    if stamped:
        return str(stamped)
    try:
        with open(os.path.join(target, VERSION_NAME), "r", encoding="utf-8") as fh:
            baked = fh.read().strip()
        if baked:
            return baked
    except OSError:
        pass
    if os.environ.get("MATRX_IMAGE_VERSION"):
        return os.environ["MATRX_IMAGE_VERSION"]
    try:
        with open("/etc/sandbox-image-version", "r", encoding="utf-8") as fh:
            baked = fh.read().strip()
        if baked:
            return baked
    except OSError:
        pass
    return "unknown"


# ── daemon ───────────────────────────────────────────────────────────────────


def _daemon_pids() -> list[int]:
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        if DAEMON_MARKER in cmdline and "uvicorn" in cmdline:
            pids.append(int(entry))
    return pids


def _daemon_healthy(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(DAEMON_HEALTH, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def restart_daemon(wait_seconds: float = 60.0) -> dict:
    """Stop and relaunch the in-container daemon, exactly as the entrypoint does.

    Destroys PTY sessions (they are ``pty.fork()`` children of this process) and
    the in-process cloud-sync watcher; detached user processes are unaffected.
    Callers decide whether that is acceptable — this function just does it.
    """
    if os.geteuid() != 0:
        return {"status": "skipped", "reason": "daemon restart needs root; rerun with sudo"}
    pids = _daemon_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 10
    while time.time() < deadline and any(_alive(pid) for pid in pids):
        time.sleep(0.2)
    for pid in pids:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    launch = (
        "cd /home/agent && PYTHONDONTWRITEBYTECODE=1 "
        "python3 -m uvicorn matrx_agent.api.main:app --host 0.0.0.0 --port 8000 "
        ">> /var/log/sandbox/api.log 2>&1 &"
    )
    try:
        subprocess.run(
            ["sudo", "-E", "-u", "agent", "bash", "-c", launch],
            check=True,
            timeout=30,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"status": "failed", "reason": f"relaunch failed: {exc}", "stopped_pids": pids}
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _daemon_healthy():
            return {"status": "restarted", "stopped_pids": pids}
        time.sleep(0.5)
    return {"status": "failed", "reason": "daemon did not become healthy after restart", "stopped_pids": pids}


def _alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


# ── the install ──────────────────────────────────────────────────────────────


#: Paths the installer must never write into. The first entry is the law: the
#: user's home is retained across the container's whole life and is not ours.
_FORBIDDEN_PREFIXES = ("/home/", "/root/", "/data/", "/mnt/", "/media/", "/srv/")
_FORBIDDEN_EXACT = {"/", "/home", "/root", "/opt", "/usr", "/etc", "/var", "/bin", "/lib", "/data"}


def _assert_safe_target(target: str) -> None:
    """The SDK lives in the system area. Refuse anything that is, or is inside,
    a user's retained data — a refresh that writes there is a data-loss bug, not
    an upgrade."""
    resolved = os.path.abspath(target)
    if resolved in _FORBIDDEN_EXACT or resolved.startswith(_FORBIDDEN_PREFIXES):
        raise RefuseToInstall(
            f"refusing to install the SDK into {resolved!r}: retained user data and system "
            "roots are never install targets. The SDK lives at /opt/sandbox/sdk."
        )


def _requirements(root: str) -> set[str]:
    """Third-party distribution names the tree declares, so a dependency-level
    change is reported rather than silently half-installed."""
    names = set()
    try:
        with open(os.path.join(root, "pyproject.toml"), "r", encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        return names
    block = body.split("dependencies = [", 1)
    if len(block) < 2:
        return names
    for line in block[1].split("]", 1)[0].splitlines():
        line = line.strip().strip(",").strip('"').strip("'")
        if not line or line.startswith("#"):
            continue
        name = line.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
        if name:
            names.add(name.lower())
    return names


def apply(
    source: str = DEFAULT_SOURCE,
    target: str = DEFAULT_TARGET,
    *,
    image_id: str = "",
    image_version: str = "",
    allow_daemon_restart: bool = False,
) -> dict:
    """Install ``source`` as the box's SDK. Returns the full JSON-able result."""
    _assert_safe_target(target)
    started = time.time()
    result: dict = {
        "action": "sdk_self_update",
        "status": "failed",
        "from": installed_version(target),
        "to": image_version or "unknown",
        "image_id": image_id,
        "target": target,
        "source": source,
        "daemon_restart": {"status": "not_needed"},
        "deps_checked": {"added": [], "removed": []},
    }
    if not os.path.isdir(source):
        # Nothing to install is the NORMAL case on demand: only the orchestrator
        # can reach the image, and it removes the payload after installing it.
        # Say what actually fixes it instead of a bare failure.
        result["reason"] = (
            f"no SDK staged at {source}. Only the orchestrator can fetch the current SDK "
            "(it holds the image); it stages and installs one on every agent binding when "
            "this box is behind. Rebind this sandbox to get the newest tools, or pass "
            "--source <dir> if you have a tree already. `mtx self-update --status` shows "
            "what this box is carrying."
        )
        return result
    if not os.path.isfile(os.path.join(source, "matrx_agent", "api", "main.py")):
        result["reason"] = f"{source} does not look like an SDK tree (matrx_agent/api/main.py missing)"
        return result

    pre_healthy = _daemon_healthy()
    old_daemon = daemon_digest(target)
    new_daemon = daemon_digest(source)
    old_all = tree_digest(target)
    new_all = tree_digest(source)
    old_reqs, new_reqs = _requirements(target), _requirements(source)
    result["deps_checked"] = {
        "added": sorted(new_reqs - old_reqs),
        "removed": sorted(old_reqs - new_reqs),
    }

    if old_all and old_all == new_all:
        result.update(status="already_current", reason="the staged SDK is byte-identical to the installed one")
        _stamp(target, image_id, image_version, new_all)
        _ensure_shim(result)
        result["elapsed_seconds"] = round(time.time() - started, 3)
        return result

    prev = target + ".prev"
    staged = target + ".staging"
    try:
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(source, staged, symlinks=True)
        _write_version(staged, image_version)
        shutil.rmtree(prev, ignore_errors=True)
        if os.path.isdir(target):
            try:
                os.rename(target, prev)
                result["swap_mode"] = "rename"
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # A tree that came from a lower overlayfs layer cannot be
                # renamed — which is exactly the case on a box running its
                # original image. Copy the old tree aside instead, then swap.
                # The replacement is still one rename, so the window in which
                # /opt/sandbox/sdk is absent is the rmtree, not a file-by-file
                # overwrite: no import can ever see a half-merged tree.
                shutil.copytree(target, prev, symlinks=True)
                shutil.rmtree(target)
                result["swap_mode"] = "replace"
        os.rename(staged, target)
    except OSError as exc:
        # Nothing was swapped, or the swap failed after the old tree moved:
        # put it back rather than leaving the box without an SDK.
        if not os.path.isdir(target) and os.path.isdir(prev):
            try:
                os.rename(prev, target)
            except OSError:
                try:
                    shutil.copytree(prev, target, symlinks=True)
                except OSError:
                    pass
        shutil.rmtree(staged, ignore_errors=True)
        result["reason"] = f"install failed: {exc}"
        return result

    _purge_pycache(target)
    _stamp(target, image_id, image_version, new_all)
    _ensure_shim(result)

    if new_daemon != old_daemon:
        if allow_daemon_restart:
            result["daemon_restart"] = restart_daemon()
            if result["daemon_restart"].get("status") == "failed" and pre_healthy:
                # We stopped a daemon that WAS working and the new code will not
                # come up (a dependency the file copy cannot install, most
                # likely). Put the box back the way we found it — a broken
                # daemon is worse than an old one.
                result["rollback"] = _rollback(target, prev)
                result["status"] = "rolled_back"
                result["elapsed_seconds"] = round(time.time() - started, 3)
                return result
        else:
            result["daemon_restart"] = {
                "status": "deferred",
                "reason": (
                    "the daemon's own code changed, but a restart drops live terminal "
                    "sessions; it will pick the new code up on its next start. The mtx "
                    "CLI is already running the new code."
                ),
            }
    result["status"] = "refreshed"
    result["elapsed_seconds"] = round(time.time() - started, 3)
    return result


def _rollback(target: str, prev: str) -> dict:
    """Put the previous SDK back and restart on it. Returns what happened."""
    failed = f"{target}.failed-{int(time.time())}"
    try:
        if not os.path.isdir(prev):
            return {"status": "impossible", "reason": f"no previous tree at {prev}"}
        try:
            os.rename(target, failed)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.copytree(target, failed, symlinks=True)
            shutil.rmtree(target)
        os.rename(prev, target)
    except OSError as exc:
        return {"status": "failed", "reason": f"rollback rename failed: {exc}"}
    restarted = restart_daemon()
    return {
        "status": "restored" if restarted.get("status") == "restarted" else "restored_daemon_down",
        "restored_from": prev,
        "failed_tree_kept_at": failed,
        "daemon": restarted.get("status"),
    }


def _write_version(root: str, image_version: str) -> None:
    if not image_version:
        return
    try:
        with open(os.path.join(root, VERSION_NAME), "w", encoding="utf-8") as fh:
            fh.write(image_version + "\n")
    except OSError:
        pass


def _stamp(target: str, image_id: str, image_version: str, digest: str) -> None:
    payload = {
        "to_image_id": image_id,
        "to_version": image_version,
        "sdk_digest": digest,
        "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        with open(os.path.join(target, STAMP_NAME), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError:
        pass


def _purge_pycache(root: str) -> None:
    for dirpath, dirnames, _ in os.walk(root):
        for name in list(dirnames):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
                dirnames.remove(name)


def _write_shim(path: str, body: str, key: str, result: dict) -> None:
    """Create one ``/usr/local/bin`` shim, once, and say which way it went.

    Only ever CREATE. A box whose shim points at a different interpreter is a
    box where that command works today; "correcting" it would break the command
    this refresh exists to deliver.
    """
    if os.path.exists(path):
        result[key] = "present"
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        result[key] = "installed"
    except OSError as exc:
        result[key] = f"failed: {exc}"


def _ensure_shim(result: dict) -> None:
    """A box born before a shim existed gets it here, or it could never type
    the command the refresh just installed.

    ``browse`` joined ``mtx`` on 2026-09-18: the browser CLI ships in the image
    from that build on, and this is how every EXISTING box gets it without a
    migration (boxes are never force-migrated — see the toolchain note in
    ``matrx_agent/cli/toolchain.py``).
    """
    _write_shim(MTX_SHIM, MTX_SHIM_BODY, "mtx_shim", result)
    _write_shim(BROWSE_SHIM, BROWSE_SHIM_BODY, "browse_shim", result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mtx self-update",
        description="Install the current sandbox SDK into this box (never touches your home).",
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE, help=f"staged SDK tree (default {DEFAULT_SOURCE})")
    parser.add_argument("--target", default=DEFAULT_TARGET, help=f"install location (default {DEFAULT_TARGET})")
    parser.add_argument("--image-id", default="", help="image identity to stamp")
    parser.add_argument("--image-version", default="", help="image version to stamp")
    parser.add_argument(
        "--allow-daemon-restart",
        action="store_true",
        help="restart the in-container daemon when its code changed (drops live terminal sessions)",
    )
    parser.add_argument("--status", action="store_true", help="print what SDK this box carries and exit")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(
            {
                "installed_version": installed_version(args.target),
                "stamp": read_stamp(args.target),
                "daemon_running": bool(_daemon_pids()),
            },
            indent=2,
        ))
        return 0

    try:
        result = apply(
            args.source,
            args.target,
            image_id=args.image_id,
            image_version=args.image_version,
            allow_daemon_restart=args.allow_daemon_restart,
        )
    except RefuseToInstall as exc:
        print(json.dumps({"action": "sdk_self_update", "status": "refused", "reason": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("refreshed", "already_current") else 1


if __name__ == "__main__":
    sys.exit(main())
