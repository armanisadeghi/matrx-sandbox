"""Session manifest — what was the sandbox doing.

Schema is documented in ``docs/PERSISTENCE_PLAN.md`` §4.4. The manifest is
written to ``/home/agent/.matrx/session.json`` periodically by the
``CheckpointDaemon`` and atomically on shutdown. Shape kept stable and
versioned (``schema_version``) so future sandboxes can read manifests written
by older containers.

Fail-soft: if any field collection raises (e.g. /proc unavailable, FS error),
we record the failure in ``transient_things_we_could_not_save`` and keep the
rest of the manifest intact.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
HOME = Path(os.environ.get("HOT_PATH", "/home/agent"))
MATRX_DIR = HOME / ".matrx"
MANIFEST_PATH = MATRX_DIR / "session.json"
PRIOR_MANIFEST_PATH = MATRX_DIR / "session.prev.json"
LOCK_DIR = MATRX_DIR / "locks"

# Files / directories we never include in any size or content reporting —
# performance + privacy. Ignored consistently across find/du.
IGNORED_NAMES = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".cache"}
IGNORED_PATH_SUFFIXES = (".pyc", ".lock")


@dataclass
class RepoInfo:
    path: str
    remote_url: str | None = None
    default_remote: str = "origin"
    branch: str | None = None
    head_sha: str | None = None
    ahead: int = 0
    behind: int = 0
    clean: bool = True
    uncommitted_files: list[dict[str, str]] = field(default_factory=list)
    auto_stash: dict[str, Any] | None = None


@dataclass
class ProcessInfo:
    pid: int
    command: str
    cwd: str | None = None
    rss_kb: int = 0


@dataclass
class SessionManifest:
    schema_version: int
    user_id: str
    tier: str
    sandbox_id: str
    captured_at: str
    graceful: bool
    shell_cwd: str | None = None
    shell_history_tail: list[str] = field(default_factory=list)
    repos: list[RepoInfo] = field(default_factory=list)
    processes_at_shutdown: list[ProcessInfo] = field(default_factory=list)
    ports_at_shutdown: list[int] = field(default_factory=list)
    transient_things_we_could_not_save: list[str] = field(default_factory=list)
    size: dict[str, Any] = field(default_factory=dict)
    # Spliced in by main.py on shutdown — watcher stats so the session report
    # can render "files synced this session".
    cloud_sync: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(fn, *args, **kwargs):
    """Run ``fn``; capture exceptions and return (result, error_str)."""
    try:
        return fn(*args, **kwargs), None
    except Exception as e:  # noqa: BLE001 — manifest must never crash the daemon
        return None, f"{fn.__name__ if callable(fn) else 'op'}: {e!r}"


def _git(args: list[str], cwd: str | os.PathLike, timeout: float = 5.0) -> str | None:
    """Best-effort git invocation — returns trimmed stdout on success, None on any failure."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if r.returncode != 0:
            return None
        return r.stdout.strip()
    except Exception:
        return None


def _find_git_repos(root: Path, max_depth: int = 6) -> list[Path]:
    """Walk ``root`` for ``.git`` directories, capping depth + skipping junk."""
    found: list[Path] = []
    root = root.resolve()
    if not root.exists():
        return found

    def _walk(p: Path, depth: int):
        if depth > max_depth:
            return
        if p.name in IGNORED_NAMES:
            return
        try:
            entries = list(p.iterdir())
        except (PermissionError, FileNotFoundError, OSError):
            return
        # ``.git`` can be a directory (regular repo) or file (worktree / submodule)
        for entry in entries:
            if entry.name == ".git" and (entry.is_dir() or entry.is_file()):
                found.append(p)
                # Don't descend into a repo's own subdirs — submodules are separate
                # walk roots and would explode the search; leave that for v2.
                return
        for entry in entries:
            if entry.is_dir() and entry.name not in IGNORED_NAMES:
                _walk(entry, depth + 1)

    _walk(root, 0)
    return found


def _collect_repo_info(repo_dir: Path) -> RepoInfo:
    info = RepoInfo(path=str(repo_dir))
    remote = _git(["remote", "get-url", "origin"], repo_dir)
    if remote:
        info.remote_url = remote
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
    if branch and branch != "HEAD":
        info.branch = branch
    head = _git(["rev-parse", "HEAD"], repo_dir)
    if head:
        info.head_sha = head[:12]

    ahead_behind = _git(
        ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        repo_dir,
    )
    if ahead_behind:
        try:
            behind, ahead = (int(x) for x in ahead_behind.split())
            info.ahead = ahead
            info.behind = behind
        except ValueError:
            pass

    porcelain = _git(["status", "--porcelain"], repo_dir)
    if porcelain:
        info.clean = False
        for line in porcelain.splitlines():
            if not line:
                continue
            status_code = line[:2].strip() or "?"
            path = line[3:] if len(line) > 3 else line
            info.uncommitted_files.append({"path": path, "status": status_code})
    return info


def _collect_processes(limit: int = 50) -> list[ProcessInfo]:
    """Walk /proc — only entries owned by user 'agent', excluding the daemon itself."""
    out: list[ProcessInfo] = []
    self_pid = os.getpid()
    proc = Path("/proc")
    if not proc.exists():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        try:
            with (entry / "comm").open() as f:
                comm = f.read().strip()
            with (entry / "cmdline").open("rb") as f:
                raw = f.read().decode("utf-8", errors="replace")
                cmdline = raw.replace("\x00", " ").strip() or comm
            with (entry / "status").open() as f:
                status_text = f.read()
            rss_kb = 0
            for line in status_text.splitlines():
                if line.startswith("VmRSS:"):
                    try:
                        rss_kb = int(line.split()[1])
                    except (IndexError, ValueError):
                        rss_kb = 0
                    break
            cwd = None
            try:
                cwd = os.readlink(entry / "cwd")
            except OSError:
                pass
            out.append(ProcessInfo(pid=pid, command=cmdline[:512], cwd=cwd, rss_kb=rss_kb))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if len(out) >= limit:
            break
    return out


def _collect_listening_ports() -> list[int]:
    """Parse /proc/net/tcp{,6} for listening ports (state 0A)."""
    ports: set[int] = set()
    for fname in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(fname) as f:
                lines = f.readlines()[1:]
            for line in lines:
                parts = line.split()
                if len(parts) < 4:
                    continue
                if parts[3] != "0A":  # 0A = LISTEN
                    continue
                local = parts[1]
                _, port_hex = local.rsplit(":", 1)
                ports.add(int(port_hex, 16))
        except (OSError, ValueError):
            continue
    return sorted(ports)


def _collect_shell_history(limit: int = 200) -> list[str]:
    """Tail of bash/zsh history for the agent user. No secrets filter — we
    expect users to have ``HISTIGNORE`` set if they care; we just truncate.
    """
    candidates = [HOME / ".bash_history", HOME / ".zsh_history"]
    for path in candidates:
        try:
            if not path.exists():
                continue
            with path.open(encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail = [line.rstrip("\n") for line in lines[-limit:] if line.strip()]
            return tail
        except OSError:
            continue
    return []


def _collect_size_summary() -> dict[str, Any]:
    """Cheap home dir size — uses du with caps."""
    summary: dict[str, Any] = {"home_bytes": 0, "biggest_dirs": []}
    try:
        # First pass: total size, ignoring known-noisy dirs.
        excludes = []
        for name in IGNORED_NAMES:
            excludes.extend(["--exclude", name])
        result = subprocess.run(
            ["du", "-sb", str(HOME)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0 and result.stdout:
            try:
                summary["home_bytes"] = int(result.stdout.split()[0])
            except (IndexError, ValueError):
                pass

        # Second pass: top ~5 biggest direct subdirs (1 level only — fast).
        result2 = subprocess.run(
            ["du", "-sb", *[str(p) for p in HOME.iterdir() if p.is_dir()]],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result2.returncode == 0:
            entries = []
            for line in result2.stdout.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    try:
                        entries.append({"path": parts[1], "size_bytes": int(parts[0])})
                    except ValueError:
                        continue
            entries.sort(key=lambda x: x["size_bytes"], reverse=True)
            summary["biggest_dirs"] = entries[:5]
    except Exception as e:  # noqa: BLE001
        summary.setdefault("error", str(e))
    return summary


def collect_manifest(graceful: bool, sandbox_id: str | None = None,
                     user_id: str | None = None, tier: str | None = None) -> SessionManifest:
    """Build a manifest snapshot of the current sandbox state. Read-only."""
    sandbox_id = sandbox_id or os.environ.get("SANDBOX_ID", "unknown")
    user_id = user_id or os.environ.get("USER_ID", "unknown")
    tier = tier or os.environ.get("MATRX_TIER", "unknown")

    failures: list[str] = []

    repos: list[RepoInfo] = []
    try:
        for repo_dir in _find_git_repos(HOME):
            try:
                repos.append(_collect_repo_info(repo_dir))
            except Exception as e:  # noqa: BLE001
                failures.append(f"repo info for {repo_dir}: {e!r}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"git scan: {e!r}")

    procs, err = _safe(_collect_processes)
    if err:
        failures.append(err)
    procs = procs or []

    ports, err = _safe(_collect_listening_ports)
    if err:
        failures.append(err)
    ports = ports or []

    history, err = _safe(_collect_shell_history)
    if err:
        failures.append(err)
    history = history or []

    size, err = _safe(_collect_size_summary)
    if err:
        failures.append(err)
    size = size or {}

    # Always add documented "things we don't preserve" so users see the truth
    # even when collection fully succeeds.
    failures.extend([
        "running processes (resumed manually if needed)",
        "shell environment variables set with `export`",
        "files outside /home/agent/ (root FS, /tmp, /etc, /var)",
        "open editor tabs (matrx-frontend handles those separately)",
    ])

    return SessionManifest(
        schema_version=SCHEMA_VERSION,
        user_id=user_id,
        tier=tier,
        sandbox_id=sandbox_id,
        captured_at=_now(),
        graceful=graceful,
        shell_cwd=str(HOME),
        shell_history_tail=history,
        repos=repos,
        processes_at_shutdown=procs,
        ports_at_shutdown=ports,
        transient_things_we_could_not_save=failures,
        size=size,
    )


def write_manifest(manifest: SessionManifest) -> None:
    """Atomically write the manifest under /home/agent/.matrx/session.json.

    Atomic = write to ``session.json.tmp`` + ``os.replace``. We rotate the
    previous manifest to ``session.prev.json`` so the next session's startup
    can render a "what we restored" report from it.
    """
    MATRX_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)

    # Convert nested dataclasses; truncate to keep manifest under 1 MB.
    payload = asdict(manifest)
    blob = json.dumps(payload, indent=2, default=str)
    if len(blob) > 1_000_000:
        # Drop history first, then biggest-dirs, then process list.
        manifest.shell_history_tail = manifest.shell_history_tail[-50:]
        manifest.size = {"home_bytes": manifest.size.get("home_bytes", 0)}
        manifest.processes_at_shutdown = manifest.processes_at_shutdown[:10]
        payload = asdict(manifest)
        blob = json.dumps(payload, indent=2, default=str)

    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(blob)

    # Rotate: current → prev (only if current exists)
    if MANIFEST_PATH.exists():
        try:
            shutil.copy2(MANIFEST_PATH, PRIOR_MANIFEST_PATH)
        except OSError as e:
            logger.warning("Failed to rotate prior manifest: %s", e)

    os.replace(tmp, MANIFEST_PATH)
    logger.debug("Wrote manifest to %s (%d bytes)", MANIFEST_PATH, len(blob))


def read_prior_manifest() -> dict[str, Any] | None:
    """Return the previous session's manifest if one exists.

    Looks first at ``session.prev.json`` (rotated by the most-recent shutdown)
    then falls back to ``session.json`` (in case shutdown didn't rotate).
    """
    for path in (PRIOR_MANIFEST_PATH, MANIFEST_PATH):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Couldn't read manifest %s: %s", path, e)
    return None
