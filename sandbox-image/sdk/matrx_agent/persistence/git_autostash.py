"""Git auto-stash on shutdown — never silently drop uncommitted work.

For each git repo under ``/home/agent/`` with a dirty working tree:

  1. ``git add -A``
  2. ``git stash push --include-untracked --message "matrx-auto-stash {ts}"``
     — preserves locally; stash refs live in ``.git/refs/stash`` which IS in
     the persisted home dir, so the next session sees them via ``git stash list``.
  3. If ``git ls-remote origin`` works AND credentials are configured (via
     ``/credentials`` or ``GH_TOKEN`` env), build a real branch on top of the
     stash and push it: ``matrx/auto-stash/{YYYY-MM-DD-HHMM}``. That gives us
     a remote backup independent of the local FS.
  4. Record exactly what we did in the manifest so the next session's report
     is honest.

We never block shutdown longer than ~30 s total (across all repos).
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _git(args: list[str], cwd: str | Path, timeout: float = 8.0) -> tuple[bool, str, str]:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", f"git {args[0]} timed out"
    except Exception as e:  # noqa: BLE001
        return False, "", str(e)


def _is_dirty(repo: Path) -> bool:
    ok, out, _ = _git(["status", "--porcelain"], repo)
    return ok and bool(out.strip())


def _has_pushable_remote(repo: Path) -> bool:
    """Check whether ``origin`` exists AND ``ls-remote`` succeeds (== creds work)."""
    ok, out, _ = _git(["remote", "get-url", "origin"], repo, timeout=3.0)
    if not ok or not out:
        return False
    ok2, _, _ = _git(["ls-remote", "--exit-code", "origin", "HEAD"], repo, timeout=8.0)
    return ok2


def auto_stash_repo(repo: Path, push_remote: bool = True) -> dict[str, Any] | None:
    """Stash dirty work in one repo. Returns the manifest fragment, or None if clean.

    Tightly time-budgeted (~12 s per repo) and never raises — a failed
    auto-stash is recorded as ``error`` in the returned dict.
    """
    if not _is_dirty(repo):
        return None

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    stash_message = f"matrx-auto-stash {ts}"
    branch_name = f"matrx/auto-stash/{ts}"

    result: dict[str, Any] = {
        "stashed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stash_message": stash_message,
        "stash_ref": None,
        "pushed_branch": None,
        "pushed_to_remote": False,
        "error": None,
    }

    # Stage everything so untracked files end up in the stash too.
    _git(["add", "-A"], repo)

    ok, _, err = _git(
        ["stash", "push", "--include-untracked", "--message", stash_message],
        repo,
        timeout=15.0,
    )
    if not ok:
        result["error"] = f"git stash failed: {err or 'unknown'}"
        return result

    # Capture the stash ref so the next session's report can mention it.
    ok, refs_out, _ = _git(["stash", "list", "--max-count=1"], repo)
    if ok and refs_out:
        # Format: "stash@{0}: On branch: matrx-auto-stash 2026-04-26-2130"
        result["stash_ref"] = refs_out.split(":", 1)[0].strip()

    if not push_remote or not _has_pushable_remote(repo):
        return result

    # Build a real commit on top of HEAD that contains the stash's tree, then
    # push as a fresh branch. The stash itself remains for ``git stash pop``.
    ok, head_sha, _ = _git(["rev-parse", "HEAD"], repo)
    ok2, stash_tree, _ = _git(["rev-parse", "stash@{0}^{tree}"], repo)
    if not (ok and ok2):
        result["error"] = "couldn't resolve HEAD or stash tree for push"
        return result

    commit_msg = (
        f"matrx auto-stash {ts}\n\n"
        "Captured by the Matrx sandbox shutdown handler. Restore by checking out\n"
        f"this branch and applying or by running ``git stash pop`` on stash@{{0}}.\n"
        f"\nThis branch was created from HEAD={head_sha[:12]} + stash@{{0}}^{{tree}}."
    )
    ok, new_commit, err = _git(
        [
            "commit-tree",
            stash_tree,
            "-p", head_sha,
            "-m", commit_msg,
        ],
        repo,
    )
    if not ok or not new_commit:
        result["error"] = f"commit-tree failed: {err or 'unknown'}"
        return result

    ok, _, err = _git(["update-ref", f"refs/heads/{branch_name}", new_commit], repo)
    if not ok:
        result["error"] = f"update-ref failed: {err or 'unknown'}"
        return result

    ok, _, err = _git(
        ["push", "--no-verify", "origin", f"refs/heads/{branch_name}:refs/heads/{branch_name}"],
        repo,
        timeout=20.0,
    )
    if ok:
        result["pushed_branch"] = branch_name
        result["pushed_to_remote"] = True
    else:
        result["error"] = f"push failed: {err or 'unknown'}"

    return result


def auto_stash_all_repos(repos: list[Path], push_remote: bool = True,
                        total_budget_seconds: float = 30.0) -> dict[str, dict[str, Any]]:
    """Auto-stash every dirty repo, time-budgeted across the whole batch.

    Returns ``{repo_path: stash_result}`` for repos we actually stashed.
    """
    results: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + total_budget_seconds
    for repo in repos:
        if time.monotonic() > deadline:
            results[str(repo)] = {"error": "skipped — auto-stash budget exhausted"}
            continue
        try:
            r = auto_stash_repo(repo, push_remote=push_remote)
        except Exception as e:  # noqa: BLE001
            r = {"error": f"unexpected: {e!r}"}
        if r is not None:
            results[str(repo)] = r
    return results
