"""Start-of-session repo tidy + session brief.

Why this exists (tracker 1734465c): checkouts under ``~/repos`` live on the
persistent home volume, so nothing in the image ever refreshes them. On
2026-10-21 two of them were found on long-merged, remote-deleted feature
branches 25k and 19k commits behind ``main``, and the agent began work with no
idea of repo state, image age or drift.

Two halves, both safe to run at any time and from any trigger:

* :func:`tidy_repo` — fetch with prune, then at most two *safe by construction*
  moves: (1) leave a branch whose upstream is gone, that is fully merged into
  the default branch and carries no local changes or unpushed commits, for the
  default branch; (2) fast-forward the checked-out default branch from
  ``origin`` when the tracked tree is clean and the move is ff-only. Anything
  else — tracked changes, unpushed or diverged commits, a detached HEAD, an
  operation in progress, a held index lock — is *reported*, never touched.
  Stashes are never read beyond a count. Untracked files do not block a move:
  git itself refuses a checkout or merge that would overwrite one.
* :func:`build_brief` — a short Markdown + JSON picture of the box: every
  repo's branch, ahead/behind, dirty/untracked/stash counts, last commits and
  what the tidy did; the image version, age and drift; the toolchain; and the
  open warnings. Written to ``~/.matrx/session-brief.md`` and
  ``~/.matrx/session-brief.json`` (atomic replace), which aidream reads through
  the sandbox bridge into the agent's start-of-session context.

Triggers: the daemon's post-commit activation (boot, runs as ``agent``), the
orchestrator's binding hook (``mtx session start --detach`` as ``agent``), and
on demand (``mtx brief`` / ``mtx repos tidy``). Every git call is bounded, all
repos run in parallel, and network failures are reported, not fatal.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

HOME = Path(os.environ.get("MATRX_AGENT_HOME") or os.environ.get("HOME") or "/home/agent")
BRIEF_DIR = Path(os.environ.get("MATRX_BRIEF_DIR") or (HOME / ".matrx"))
BRIEF_MD = BRIEF_DIR / "session-brief.md"
BRIEF_JSON = BRIEF_DIR / "session-brief.json"
LOCK_PATH = BRIEF_DIR / "runtime" / "session-brief.lock"
LOG_PATH = BRIEF_DIR / "runtime" / "session-brief.log"

#: Where repos are looked for: ``~/repos/*`` plus the SDK's workspace root
#: convention (``MATRX_WORKSPACE_ROOT`` / ``HOT_PATH``) one level deep, and
#: ``~/projects/*``. Override with ``MATRX_BRIEF_REPO_ROOTS`` (os.pathsep list).
DEFAULT_REPO_ROOTS = ("repos", "projects")

FETCH_TIMEOUT_S = float(os.environ.get("MATRX_BRIEF_FETCH_TIMEOUT", "30"))
GIT_TIMEOUT_S = float(os.environ.get("MATRX_BRIEF_GIT_TIMEOUT", "15"))
REPO_DEADLINE_S = float(os.environ.get("MATRX_BRIEF_REPO_DEADLINE", "60"))
MAX_WORKERS = int(os.environ.get("MATRX_BRIEF_WORKERS", "6"))
RECENT_COMMITS = 3
#: Image drift above this many commits, or age above this many days, warns.
DRIFT_WARN_COMMITS = 100
AGE_WARN_DAYS = 30

_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
    "SSH_ASKPASS": "/bin/true",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
    "LC_ALL": "C",
}


# ─────────────────────────────────────────────────────────────────────────────
# git plumbing
# ─────────────────────────────────────────────────────────────────────────────

class GitTimeout(Exception):
    pass


def _git(repo: Path, *args: str, timeout: float = GIT_TIMEOUT_S, check: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(_GIT_ENV)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout(f"git {' '.join(args[:2])} timed out after {timeout:.0f}s") from exc
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {_first_line(proc.stderr) or proc.returncode}")
    return proc


def _out(repo: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> str | None:
    proc = _git(repo, *args, timeout=timeout)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return ""


def _git_dir(repo: Path) -> Path:
    gd = _out(repo, "rev-parse", "--absolute-git-dir")
    return Path(gd) if gd else repo / ".git"


def discover_repos(roots: list[Path] | None = None) -> list[Path]:
    """Git working trees directly under each root (and each root itself)."""
    if roots is None:
        env_roots = os.environ.get("MATRX_BRIEF_REPO_ROOTS")
        if env_roots:
            roots = [Path(p).expanduser() for p in env_roots.split(os.pathsep) if p]
        else:
            roots = [HOME / r for r in DEFAULT_REPO_ROOTS]
            ws = os.environ.get("MATRX_WORKSPACE_ROOT") or os.environ.get("HOT_PATH")
            if ws and Path(ws) != HOME:
                roots.append(Path(ws))
    seen: set[str] = set()
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates = [root]
        try:
            candidates += sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            continue
        for cand in candidates:
            if (cand / ".git").exists():
                key = str(cand.resolve())
                if key not in seen:
                    seen.add(key)
                    found.append(cand)
    return found


# ─────────────────────────────────────────────────────────────────────────────
# per-repo state + tidy
# ─────────────────────────────────────────────────────────────────────────────

def _status_counts(repo: Path) -> dict[str, int]:
    out = _git(repo, "status", "--porcelain=v1", "--untracked-files=normal").stdout
    tracked = untracked = conflicted = 0
    for line in out.splitlines():
        if not line:
            continue
        code = line[:2]
        if code == "??":
            untracked += 1
        else:
            tracked += 1
            if "U" in code or code in ("AA", "DD"):
                conflicted += 1
    return {"dirty": tracked, "untracked": untracked, "conflicted": conflicted}


def _operation_in_progress(git_dir: Path) -> str | None:
    for marker, name in (
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase/am"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    ):
        if (git_dir / marker).exists():
            return name
    return None


def _default_branch(repo: Path, remote: str = "origin") -> str | None:
    ref = _out(repo, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD")
    if ref and ref.startswith(f"refs/remotes/{remote}/"):
        return ref[len(f"refs/remotes/{remote}/"):]
    for cand in ("main", "master", "develop"):
        if _git(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{cand}").returncode == 0:
            return cand
    return None


def _ahead_behind(repo: Path, left: str, right: str) -> tuple[int, int] | None:
    """(ahead, behind) of ``left`` relative to ``right``."""
    out = _out(repo, "rev-list", "--left-right", "--count", f"{left}...{right}")
    if not out:
        return None
    try:
        a, b = out.split()
        return int(a), int(b)
    except ValueError:
        return None


def _recent_commits(repo: Path, n: int = RECENT_COMMITS) -> list[str]:
    out = _out(repo, "log", f"-{n}", "--no-color", "--format=%h %cs %s") or ""
    return [line[:90] for line in out.splitlines() if line.strip()]


def tidy_repo(repo: Path, *, fetch: bool = True, apply: bool = True,
              deadline_s: float = REPO_DEADLINE_S) -> dict[str, Any]:
    """Inspect (and, when ``apply``, safely tidy) one repo. Never raises."""
    started = time.monotonic()
    info: dict[str, Any] = {
        "path": str(repo),
        "name": repo.name,
        "actions": [],
        "notes": [],
        "warnings": [],
    }

    def remaining() -> float:
        return max(1.0, deadline_s - (time.monotonic() - started))

    try:
        _inspect_and_tidy(repo, info, fetch=fetch, apply=apply, remaining=remaining)
    except GitTimeout as exc:
        info["warnings"].append(f"timed out: {exc}; left as is")
    except Exception as exc:  # noqa: BLE001 — one repo never breaks the brief
        info["warnings"].append(f"inspection failed: {type(exc).__name__}: {_first_line(str(exc))}")
    info["elapsed_s"] = round(time.monotonic() - started, 2)
    return info


def _inspect_and_tidy(repo: Path, info: dict[str, Any], *, fetch: bool, apply: bool, remaining) -> None:
    git_dir = _git_dir(repo)
    branch = _out(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    info["branch"] = branch
    info["detached"] = branch is None
    if branch is None:
        info["head"] = _out(repo, "rev-parse", "--short", "HEAD")
    info.update(_status_counts(repo))
    stash = _out(repo, "stash", "list") or ""
    info["stashes"] = len([s for s in stash.splitlines() if s.strip()])
    op = _operation_in_progress(git_dir)
    info["operation_in_progress"] = op
    locked = (git_dir / "index.lock").exists()

    remotes = (_out(repo, "remote") or "").split()
    remote = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
    info["remote"] = remote

    # ── fetch ────────────────────────────────────────────────────────────
    fetched = False
    if remote is None:
        info["notes"].append("no remote configured")
    elif fetch:
        try:
            proc = _git(repo, "fetch", "--prune", "--quiet", remote,
                        timeout=min(FETCH_TIMEOUT_S, remaining()))
            if proc.returncode == 0:
                fetched = True
                info["actions"].append(f"fetched {remote} (prune)")
            else:
                info["warnings"].append(f"fetch failed: {_first_line(proc.stderr) or 'exit ' + str(proc.returncode)}")
        except GitTimeout as exc:
            info["warnings"].append(f"fetch {exc}")
    info["fetched"] = fetched

    default = _default_branch(repo, remote) if remote else None
    info["default_branch"] = default
    upstream = _out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") if branch else None
    configured_merge = _out(repo, "config", "--get", f"branch.{branch}.merge") if branch else None
    upstream_gone = bool(branch and configured_merge and upstream is None)
    info["upstream"] = upstream
    info["upstream_gone"] = upstream_gone

    unpushed = 0
    if branch:
        n = _out(repo, "rev-list", "--count", "HEAD", "--not", "--remotes")
        unpushed = int(n) if n and n.isdigit() else 0
    info["unpushed"] = unpushed

    if upstream:
        ab = _ahead_behind(repo, "HEAD", upstream)
        if ab:
            info["ahead"], info["behind"] = ab
    if default and remote:
        ab = _ahead_behind(repo, "HEAD", f"{remote}/{default}")
        if ab:
            info["ahead_of_default"], info["behind_default"] = ab

    blockers = []
    if info["dirty"]:
        blockers.append(f"{info['dirty']} uncommitted change(s)")
    if op:
        blockers.append(f"{op} in progress")
    if locked:
        blockers.append("index.lock held (another git command running?)")
    if info["detached"]:
        blockers.append("detached HEAD")

    # ── move 1: leave a gone, merged branch for the default branch ───────
    if branch and default and branch != default and upstream_gone:
        merged = _git(repo, "merge-base", "--is-ancestor", "HEAD", f"{remote}/{default}").returncode == 0
        reasons = list(blockers)
        if not merged:
            reasons.append(f"not merged into {remote}/{default} (squash-merged or unfinished?)")
        if unpushed:
            reasons.append(f"{unpushed} unpushed commit(s)")
        if reasons:
            info["warnings"].append(
                f"branch {branch!r} is gone upstream but was left alone: " + "; ".join(reasons)
            )
        elif not apply:
            info["notes"].append(f"would switch {branch!r} (gone upstream, merged) -> {default}")
        else:
            has_local = _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{default}").returncode == 0
            args = ["checkout", "--quiet", default] if has_local else [
                "checkout", "--quiet", "-b", default, "--track", f"{remote}/{default}"]
            proc = _git(repo, *args, timeout=min(GIT_TIMEOUT_S, remaining()))
            if proc.returncode == 0:
                info["actions"].append(
                    f"switched {branch!r} -> {default} (branch was gone upstream and merged; "
                    f"local branch kept — `git branch -d {branch}` to remove)"
                )
                info["previous_branch"] = branch
                branch = default
                info["branch"] = default
                info["upstream_gone"] = False
                info["upstream"] = _out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            else:
                info["warnings"].append(f"switch to {default} refused by git: {_first_line(proc.stderr)}")
    elif upstream_gone:
        info["warnings"].append(f"branch {branch!r} is gone upstream")

    # ── move 2: fast-forward the checked-out default branch ──────────────
    if branch and default and branch == default and remote:
        target = f"{remote}/{default}"
        ab = _ahead_behind(repo, "HEAD", target)
        if ab:
            ahead, behind = ab
            if ahead and behind:
                info["warnings"].append(f"{default} has diverged from {target} ({ahead} ahead, {behind} behind); left alone")
            elif ahead:
                info["warnings"].append(f"{default} has {ahead} unpushed commit(s)")
            elif behind:
                if blockers:
                    info["warnings"].append(f"{default} is {behind} behind {target}; not fast-forwarded: " + "; ".join(blockers))
                elif not apply:
                    info["notes"].append(f"would fast-forward {default} by {behind} commit(s)")
                else:
                    proc = _git(repo, "merge", "--ff-only", "--quiet", target,
                                timeout=min(GIT_TIMEOUT_S * 4, remaining()))
                    if proc.returncode == 0:
                        info["actions"].append(f"fast-forwarded {default} by {behind} commit(s)")
                    else:
                        info["warnings"].append(f"fast-forward refused by git: {_first_line(proc.stderr)}")
    elif branch and default and branch != default and not upstream_gone:
        if unpushed:
            info["notes"].append(f"feature branch with {unpushed} unpushed commit(s)")
        bd = info.get("behind_default")
        if bd:
            info["notes"].append(f"feature branch is {bd} commit(s) behind {remote}/{default}")

    if info["dirty"] and "uncommitted" not in " ".join(info["warnings"]):
        info["notes"].append(f"{info['dirty']} uncommitted change(s) (left alone)")
    if info["conflicted"]:
        info["warnings"].append(f"{info['conflicted']} conflicted file(s)")

    # refresh final state after any move
    if info["actions"] and any(a.startswith(("switched", "fast-forwarded")) for a in info["actions"]):
        if info.get("upstream"):
            ab = _ahead_behind(repo, "HEAD", info["upstream"])
            if ab:
                info["ahead"], info["behind"] = ab
        if default and remote:
            ab = _ahead_behind(repo, "HEAD", f"{remote}/{default}")
            if ab:
                info["ahead_of_default"], info["behind_default"] = ab
    info["recent_commits"] = _recent_commits(repo)


def tidy_all(repos: list[Path], *, fetch: bool = True, apply: bool = True) -> list[dict[str, Any]]:
    if not repos:
        return []
    results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(repos)))) as pool:
        futs = {pool.submit(tidy_repo, r, fetch=fetch, apply=apply): r for r in repos}
        for fut in concurrent.futures.as_completed(futs):
            r = futs[fut]
            try:
                results[str(r)] = fut.result()
            except Exception as exc:  # noqa: BLE001
                results[str(r)] = {"path": str(r), "name": r.name, "warnings": [f"failed: {exc}"],
                                   "actions": [], "notes": []}
    return [results[str(r)] for r in repos]


# ─────────────────────────────────────────────────────────────────────────────
# image + toolchain facts
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _cmd_version(*cmd: str) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = _first_line(proc.stdout or proc.stderr)
    return line[:48] if line else None


def image_facts(now: _dt.datetime | None = None) -> dict[str, Any]:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    version = os.environ.get("MATRX_IMAGE_VERSION") or _read("/etc/sandbox-image-version")
    facts: dict[str, Any] = {"image_version": version, "template": os.environ.get("MATRX_TEMPLATE")}
    stamp: dict[str, Any] = {}
    try:
        from matrx_agent import selfupdate  # read-only use

        stamp = selfupdate.read_stamp()
    except Exception:  # noqa: BLE001
        stamp = {}
    if stamp:
        facts["sdk_version"] = stamp.get("to_version")
        facts["sdk_refreshed_at"] = stamp.get("refreshed_at")
    else:
        facts["sdk_version"] = _read("/opt/sandbox/sdk/VERSION") or version

    # Age + drift: answered from a local matrx-sandbox checkout when it knows
    # the image commit (it always does on a dev box; otherwise "unknown").
    sandbox_repo = None
    for cand in (HOME / "repos" / "matrx-sandbox",):
        if (cand / ".git").exists():
            sandbox_repo = cand
    if version and sandbox_repo and re.fullmatch(r"[0-9a-f]{7,40}", version):
        built = _out(sandbox_repo, "show", "-s", "--format=%cI", version)
        if built:
            facts["image_commit_date"] = built
            try:
                age = now - _dt.datetime.fromisoformat(built)
                facts["image_age_days"] = age.days
            except ValueError:
                pass
        default = _default_branch(sandbox_repo) or "main"
        latest_ref = f"origin/{default}"
        n = _out(sandbox_repo, "rev-list", "--count", f"{version}..{latest_ref}")
        if n and n.isdigit():
            facts["image_commits_behind_main"] = int(n)
        sdk_v = facts.get("sdk_version")
        if sdk_v and sdk_v != version and re.fullmatch(r"[0-9a-f]{7,40}", str(sdk_v)):
            n2 = _out(sandbox_repo, "rev-list", "--count", f"{sdk_v}..{latest_ref}")
            if n2 and n2.isdigit():
                facts["sdk_commits_behind_main"] = int(n2)
    if "image_age_days" not in facts:
        try:
            mtime = os.stat("/etc/sandbox-image-version").st_mtime
            facts["image_age_days"] = (now - _dt.datetime.fromtimestamp(mtime, _dt.timezone.utc)).days
            facts["image_age_basis"] = "mtime of /etc/sandbox-image-version"
        except OSError:
            pass
    return facts


def toolchain_facts() -> dict[str, Any]:
    facts: dict[str, Any] = {
        "python": _cmd_version("python3", "--version"),
        "node": _cmd_version("node", "--version"),
        "git": _cmd_version("git", "--version"),
        "uv": _cmd_version("uv", "--version"),
        "pnpm": _cmd_version("pnpm", "--version"),
        "gh": _cmd_version("gh", "--version"),
    }
    tz_ok = False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("America/Los_Angeles")
        tz_ok = True
    except Exception:  # noqa: BLE001
        tz_ok = False
    facts["tzdata"] = tz_ok
    return facts


def box_warnings(image: dict[str, Any], tools: dict[str, Any]) -> list[str]:
    warns: list[str] = []
    behind = image.get("image_commits_behind_main")
    if isinstance(behind, int) and behind > DRIFT_WARN_COMMITS:
        warns.append(f"image is {behind} commits behind matrx-sandbox main (SDK may be refreshed; system packages are not)")
    elif not isinstance(behind, int):  # no checkout to count with: ask the SDK refresh stamp
        try:
            from matrx_agent.image_drift import warning as image_warning

            stale = image_warning()
            if stale:
                warns.append(stale)
        except Exception:  # noqa: BLE001 — the brief never fails on diagnostics
            pass
    age = image.get("image_age_days")
    if isinstance(age, int) and age > AGE_WARN_DAYS:
        warns.append(f"image is {age} days old")
    if not tools.get("tzdata"):
        warns.append("tzdata missing: zoneinfo lookups fail (pip install tzdata, or a newer image)")
    py = tools.get("python") or ""
    if re.search(r"\d(rc|a|b)\d", py):
        warns.append(f"{py} is a pre-release interpreter")
    for name in ("python", "node", "git"):
        if not tools.get(name):
            warns.append(f"{name} not found on PATH")
    if Path("/etc/matrx/bridge-env.FAILED").exists():
        warns.append("bridge env failed at boot (/etc/matrx/bridge-env.FAILED): shells may be unwired")
    try:
        du = shutil.disk_usage(str(HOME))
        pct = du.used * 100 // max(du.total, 1)
        if pct >= 90:
            warns.append(f"home volume {pct}% full")
    except OSError:
        pass
    return warns


# ─────────────────────────────────────────────────────────────────────────────
# git identity
# ─────────────────────────────────────────────────────────────────────────────
# A test once wrote user.name=Test / user.email=test@example.com into a box's
# global ~/.gitconfig and two weeks of real commits went out authored "Test".
# Nothing in the image sets a git identity, so the brief checks it every start:
# a placeholder is flagged loudly, and fixed when the platform knows who the
# person is (MATRX_GIT_USER_NAME / MATRX_GIT_USER_EMAIL in the box env, or
# ~/.matrx/identity.json {"name", "email"}). A real identity is never replaced.

PLACEHOLDER_NAMES = {"test", "test user", "tester", "your name", "user", "name", "root",
                     "ubuntu", "agent", "example", "foo", "john doe", "jane doe"}
_PLACEHOLDER_EMAIL_RE = re.compile(
    r"(^$|@example\.(com|org|net)$|^(test|you|user|foo|root|agent)@|@localhost$|@test$|\.local$)",
    re.IGNORECASE,
)
IDENTITY_FILE = BRIEF_DIR / "identity.json"
PLACEHOLDER_SCAN_DAYS = 60


def is_placeholder_name(name: str | None) -> bool:
    return not (name or "").strip() or (name or "").strip().lower() in PLACEHOLDER_NAMES


def is_placeholder_email(email: str | None) -> bool:
    return bool(_PLACEHOLDER_EMAIL_RE.search((email or "").strip()))


def _git_config(*args: str, cwd: Path | None = None) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "config", *args], cwd=str(cwd or HOME), capture_output=True, text=True,
            timeout=GIT_TIMEOUT_S, env={**os.environ, **_GIT_ENV},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


VAULT_ENV_FILE = Path("/etc/matrx/vault-env.sh")


def _vault_env_values(path: Path, names: tuple[str, ...]) -> dict[str, str]:
    import shlex

    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, raw = line.partition("=")
        if sep and key in names:
            try:
                parts = shlex.split(raw)
            except ValueError:
                continue
            if parts:
                out[key] = parts[0]
    return out


def platform_identity() -> tuple[str | None, str | None, str | None]:
    """(name, email, source) the platform published for this box's person."""
    name = os.environ.get("MATRX_GIT_USER_NAME")
    email = os.environ.get("MATRX_GIT_USER_EMAIL")
    if name and email:
        return name.strip(), email.strip(), "env MATRX_GIT_USER_NAME/EMAIL"
    # The vault env file the orchestrator publishes on binding: a detached boot
    # run may not have sourced it, so read the two names straight from it.
    vault = _vault_env_values(VAULT_ENV_FILE, ("MATRX_GIT_USER_NAME", "MATRX_GIT_USER_EMAIL"))
    if vault.get("MATRX_GIT_USER_NAME") and vault.get("MATRX_GIT_USER_EMAIL"):
        return (vault["MATRX_GIT_USER_NAME"].strip(), vault["MATRX_GIT_USER_EMAIL"].strip(),
                f"{VAULT_ENV_FILE} MATRX_GIT_USER_NAME/EMAIL")
    try:
        data = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("name") and data.get("email"):
            return str(data["name"]).strip(), str(data["email"]).strip(), str(IDENTITY_FILE)
    except (OSError, ValueError):
        pass
    return None, None, None


def git_identity_facts(repos: list[Path], *, apply: bool) -> dict[str, Any]:
    name = _git_config("--global", "user.name")
    email = _git_config("--global", "user.email")
    facts: dict[str, Any] = {"name": name, "email": email, "actions": [], "warnings": [],
                             "repo_overrides": {}, "placeholder_commits": {}}
    placeholder = is_placeholder_name(name) or is_placeholder_email(email)
    p_name, p_email, source = platform_identity()
    platform_ok = bool(p_name and p_email and not is_placeholder_name(p_name)
                       and not is_placeholder_email(p_email))
    facts["platform_identity_source"] = source if platform_ok else None
    if placeholder and platform_ok and apply:
        ok = (_git_config("--global", "user.name", p_name) is not None
              and _git_config("--global", "user.email", p_email) is not None)
        if ok:
            facts["actions"].append(
                f"replaced placeholder global git identity ({name!r} <{email}>) with {p_name} <{p_email}> from {source}")
            facts["previous"] = {"name": name, "email": email}
            facts["name"], facts["email"] = p_name, p_email
            placeholder = False
        else:
            facts["warnings"].append("could not write the global git identity")
    facts["placeholder"] = placeholder
    if placeholder:
        how = ("the platform identity is known; run `mtx repos tidy` to apply it" if platform_ok else
               "set it: git config --global user.name '<Full Name>' && git config --global user.email '<email>'"
               " (or set MATRX_GIT_USER_NAME / MATRX_GIT_USER_EMAIL in the sandbox env so start-up fixes it)")
        facts["warnings"].append(
            f"GIT IDENTITY IS A PLACEHOLDER (user.name={name!r}, user.email={email!r}): "
            f"every commit from this box is misattributed. {how}")
    for repo in repos:
        r_name = _git_config("--local", "user.name", cwd=repo)
        r_email = _git_config("--local", "user.email", cwd=repo)
        if (r_name is not None and is_placeholder_name(r_name)) or (r_email is not None and is_placeholder_email(r_email)):
            facts["repo_overrides"][repo.name] = {"name": r_name, "email": r_email}
            facts["warnings"].append(
                f"{repo.name}: repo-local git identity is a placeholder ({r_name!r} <{r_email}>), "
                f"it overrides the global one (git config --local --unset user.name / user.email)")
        try:
            proc = subprocess.run(
                ["git", "log", f"--since={PLACEHOLDER_SCAN_DAYS}.days", "-n", "500", "--format=%an%x09%ae", "HEAD"],
                cwd=str(repo), capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
                env={**os.environ, **_GIT_ENV},
            )
            bad = 0
            for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
                an, _, ae = line.partition("\t")
                if an.strip().lower() in {"test", "test user"} or (ae and is_placeholder_email(ae)):
                    bad += 1
            if bad:
                facts["placeholder_commits"][repo.name] = bad
                facts["warnings"].append(
                    f"{repo.name}: {bad} commit(s) in the last {PLACEHOLDER_SCAN_DAYS} days authored by a placeholder identity")
        except (OSError, subprocess.TimeoutExpired):
            pass
    return facts


# ─────────────────────────────────────────────────────────────────────────────
# brief
# ─────────────────────────────────────────────────────────────────────────────

def build_brief(*, fetch: bool, apply: bool, trigger: str, repos: list[Path] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    now = _dt.datetime.now(_dt.timezone.utc)
    repo_paths = discover_repos() if repos is None else repos
    repo_infos = tidy_all(repo_paths, fetch=fetch, apply=apply)
    image = image_facts(now)
    tools = toolchain_facts()
    identity = git_identity_facts(repo_paths, apply=apply)
    warnings = list(identity["warnings"]) + box_warnings(image, tools)
    for r in repo_infos:
        for w in r.get("warnings", []):
            warnings.append(f"{r['name']}: {w}")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trigger": trigger,
        "mode": "tidy" if apply else ("status+fetch" if fetch else "status"),
        "sandbox_id": os.environ.get("SANDBOX_ID"),
        "image": image,
        "toolchain": tools,
        "git_identity": identity,
        "repos": repo_infos,
        "warnings": warnings,
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def _repo_line(r: dict[str, Any]) -> str:
    bits = []
    if r.get("detached"):
        bits.append(f"DETACHED at {r.get('head')}")
    else:
        bits.append(f"`{r.get('branch')}`")
    up = r.get("upstream")
    if r.get("upstream_gone"):
        bits.append("upstream GONE")
    elif up and ("ahead" in r or "behind" in r):
        bits.append(f"↑{r.get('ahead', 0)} ↓{r.get('behind', 0)} vs {up}")
    elif not up and not r.get("detached"):
        bits.append("no upstream")
    if r.get("default_branch") and r.get("branch") != r.get("default_branch") and "behind_default" in r:
        bits.append(f"{r.get('behind_default')} behind {r.get('remote')}/{r.get('default_branch')}")
    counts = []
    for key, label in (("dirty", "dirty"), ("untracked", "untracked"), ("stashes", "stash")):
        if r.get(key):
            counts.append(f"{r[key]} {label}")
    bits.append(", ".join(counts) if counts else "clean")
    if r.get("unpushed"):
        bits.append(f"{r['unpushed']} unpushed")
    if r.get("operation_in_progress"):
        bits.append(f"{r['operation_in_progress']} in progress")
    return f"- **{r['name']}** — " + " · ".join(bits)


def render_markdown(brief: dict[str, Any]) -> str:
    img = brief.get("image") or {}
    tools = brief.get("toolchain") or {}
    lines = [
        "# Session brief",
        "",
        f"Generated {brief.get('generated_at')} ({brief.get('trigger')}, {brief.get('mode')}, "
        f"{brief.get('elapsed_s')}s). Refresh: `mtx brief` (status) · `mtx repos tidy` (fetch + safe tidy).",
        "",
    ]
    warnings = brief.get("warnings") or []
    lines.append(f"## Warnings ({len(warnings)})")
    lines += [f"- {w}" for w in warnings] or ["- none"]
    lines.append("")
    lines.append("## Repos")
    repos = brief.get("repos") or []
    if not repos:
        lines.append("- no git repos under ~/repos or ~/projects")
    for r in repos:
        lines.append(_repo_line(r))
        for a in r.get("actions", []):
            if not a.startswith("fetched"):
                lines.append(f"  - tidy: {a}")
        if r.get("fetched") is False and r.get("remote"):
            if not any(w.startswith("fetch") for w in r.get("warnings", [])):
                lines.append("  - not fetched this run")
        for n in r.get("notes", []):
            lines.append(f"  - note: {n}")
        commits = r.get("recent_commits") or []
        if commits:
            lines.append("  - recent: " + " | ".join(commits))
    lines.append("")
    ident = brief.get("git_identity") or {}
    if ident:
        lines.append("## Git identity")
        flag = " · **PLACEHOLDER**" if ident.get("placeholder") else ""
        lines.append(f"- global: {ident.get('name') or '(unset)'} <{ident.get('email') or 'unset'}>{flag}")
        for a in ident.get("actions", []):
            lines.append(f"  - fix: {a}")
        lines.append("")
    lines.append("## Image")
    img_bits = [f"version `{(img.get('image_version') or 'unknown')[:12]}`"]
    if img.get("template"):
        img_bits.append(f"template {img['template']}")
    if img.get("image_age_days") is not None:
        img_bits.append(f"{img['image_age_days']} days old")
    if img.get("image_commits_behind_main") is not None:
        img_bits.append(f"{img['image_commits_behind_main']} commits behind matrx-sandbox main")
    else:
        img_bits.append("drift unknown")
    lines.append("- " + " · ".join(img_bits))
    sdk = img.get("sdk_version")
    if sdk:
        s = f"- SDK `{str(sdk)[:12]}`"
        if img.get("sdk_refreshed_at"):
            s += f" (refreshed {img['sdk_refreshed_at']})"
        if img.get("sdk_commits_behind_main") is not None:
            s += f" · {img['sdk_commits_behind_main']} behind main"
        lines.append(s)
    lines.append("")
    lines.append("## Toolchain")
    tl = []
    for k in ("python", "node", "git", "uv", "pnpm", "gh"):
        v = tools.get(k)
        tl.append(f"{k}: {v if v else 'missing'}")
    tl.append(f"tzdata: {'ok' if tools.get('tzdata') else 'MISSING'}")
    lines.append("- " + " · ".join(tl))
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_brief(brief: dict[str, Any]) -> str:
    md = render_markdown(brief)
    _atomic_write(BRIEF_JSON, json.dumps(brief, indent=2, sort_keys=False) + "\n")
    _atomic_write(BRIEF_MD, md)
    return md


class _Lock:
    """Non-blocking exclusive lock so overlapping triggers never tidy twice."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        try:
            import fcntl

            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, "a+")
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, ImportError):
            if self.fh:
                self.fh.close()
                self.fh = None
            return False

    def release(self) -> None:
        if self.fh:
            try:
                self.fh.close()
            finally:
                self.fh = None


def run(*, fetch: bool, apply: bool, trigger: str) -> tuple[int, str | None]:
    """Build and write the brief under the lock. Returns (exit_code, markdown)."""
    lock = _Lock(LOCK_PATH)
    if not lock.acquire():
        return 75, None  # EX_TEMPFAIL: another run holds the lock
    try:
        brief = build_brief(fetch=fetch, apply=apply, trigger=trigger)
        return 0, write_brief(brief)
    finally:
        lock.release()


def spawn_detached(trigger: str, *, apply: bool = True) -> bool:
    """Start ``session start`` in a new session and return at once."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG_PATH, "a")
    except OSError:
        log = subprocess.DEVNULL  # type: ignore[assignment]
    args = [sys.executable, "-m", "matrx_agent.session_brief", "--trigger", trigger]
    if not apply:
        args.append("--no-tidy")
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
            cwd=str(HOME) if HOME.is_dir() else "/",
        )
        return True
    except OSError:
        return False
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m matrx_agent.session_brief")
    p.add_argument("--trigger", default="manual")
    p.add_argument("--no-tidy", action="store_true", help="fetch + report only, change nothing")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--print", dest="do_print", action="store_true")
    a = p.parse_args(argv)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    code, md = run(fetch=not a.no_fetch, apply=not a.no_tidy, trigger=a.trigger)
    if code == 75:
        print(f"[session-brief {stamp}] another run in progress; skipped ({a.trigger})")
        return 0
    print(f"[session-brief {stamp}] wrote {BRIEF_MD} ({a.trigger})")
    if a.do_print and md:
        print(md)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
