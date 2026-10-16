"""The unused-home sweep: a hosted home nobody ever used is deleted, eventually.

OWNER RULING (Arman, 2026-10-16): *"we need to know sandboxes that are
essentially empty and delete them after some time so users who click to create
them and never use them will not have them sitting around forever."*

A hosted home is the per-(user, organization) Docker volume
``matrx-user-<uid>[-org-<oid>]`` mounted at ``/home/agent``. It survives every
container destroy on purpose, so a person who clicked "create" once and never
came back keeps a volume forever. This pass runs inside the existing 60-second
reaper tick (no new loop, no new scheduler) and removes exactly those homes.

WHAT "NEVER USED" MEANS — every condition must hold:

1. **Hosted tier only**, and only the canonical per-(user, organization) volume
   (``storage_layout.user_volume_name``). A bind-mount home, a legacy volume
   name or a row that names some other volume is never touched.
2. **Nothing is up.** Every row of the (user, organization) — soft-deleted ones
   included — is terminal (``stopped``/``expired``/``failed``).
3. **Not always-on.** No row of the pair carries the always-on mark. An
   enrolled person's workspace is never "unused", whatever is in it: the revive
   pass would recreate it a minute later and the deletion would just be churn.
4. **Untouched for the period.** The newest of every row's ``created_at``,
   ``stopped_at`` and ``last_heartbeat_at`` is older than
   ``empty_home_delete_after_days``. (``updated_at`` is deliberately ignored:
   the retention purge stamps it when it soft-deletes, which is housekeeping,
   not use.)
5. **The home holds no user content** — :func:`_classify_home`, run read-only
   inside a networkless helper container. Only FILES are evidence; an empty
   directory never is (an untouched aidream home carries empty ``workspace/``
   and ``temp/``). Content is ANY of:
     * a file under ``projects/``, ``repos/``, ``scratch/``,
       ``.matrx/plans/``, ``.matrx/skills/``, ``.matrx/memory/`` or
       ``.matrx/runtime/shell-logs/``;
     * any agent tool-call record under ``.matrx/runtime/tool-calls/`` (the
       ``_runtime/`` service-action subdirectory excepted) — every shell or
       python call an agent makes writes one, so this is the durable
       "an agent worked here" signal that survives an orchestrator restart;
     * a shell/REPL history file at the top level (a human used a terminal);
     * a file under any non-hidden top-level entry the layout does not create;
     * in a TEMPLATE-SEEDED CLONE (:data:`TEMPLATE_SEEDED_CLONES`, e.g. the
       aidream template's ``aidream/``): uncommitted or untracked changes, a
       stash, or a commit not reachable from the clone's own upstream refs
       (``refs/remotes/*`` plus ``FETCH_HEAD`` — the helper has no network).
       A pristine seed is not use, and the clone is never weighed;
     * more than ``empty_home_max_bytes`` in total, ``cloud-files/`` and the
       template clones excluded;
     * anything the helper (or git) could not read.
   ``cloud-files/`` is a MIRROR of the person's AI Matrx files (pulled down at
   every boot; anything written in the box is pushed up within seconds), so its
   contents alone do not make a home "used"; writing there takes a tool call
   or a terminal, both of which leave evidence above.

A home with ANY user content is never deleted by this pass — it only ever
stops on the existing idle rules.

THE DELETION goes through the existing fences, never a raw Docker call around
them: the decision is re-made under the exclusive lifecycle lease on the home
(the same lease create, resume, migration and ``delete_user_volume`` take),
and the volume is removed by :func:`sandbox_manager.delete_user_volume`, which
refuses while any container — running or exited — is attached. A container
still attached to a terminal row is left for the zombie reaper and retried on a
later pass. Every row of the pair is then soft-deleted with
``config['home_deleted']`` naming why, so it can never be resumed onto a home
that no longer exists and the reason is on the row for any screen to show.

KNOBS (``infrastructure.sandbox``; seeded by aidream, never a Python default):
``empty_home_delete_after_days`` (``<= 0`` turns the sweep off),
``empty_home_delete_max_per_pass``, ``empty_home_max_bytes``. A knob that
cannot be read turns the sweep OFF for that pass and says so — the safe failure
of a deleting sweep is to delete nothing.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import textwrap
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

AFTER_DAYS_KNOB = "empty_home_delete_after_days"
MAX_PER_PASS_KNOB = "empty_home_delete_max_per_pass"
MAX_BYTES_KNOB = "empty_home_max_bytes"

#: Where the reason lives on every row of a deleted home.
HOME_DELETED_CONFIG_KEY = "home_deleted"
HOME_DELETED_REASON = "unused_empty_home"

#: A home inspection runs a helper container, so the pass runs at most this
#: often inside the 60-second tick (process-local; a restart runs it at once).
SWEEP_INTERVAL_SECONDS = 3600

TERMINAL_STATUSES = frozenset({"stopped", "expired", "failed"})

_RESULT_PREFIX = "EMPTY_HOME_RESULT="
_HELPER_LABELS = {"matrx.kind": "empty-home-inspector", "matrx.owner": "orchestrator"}

#: Top-level home directories a TEMPLATE seeds as a git clone (template name →
#: directory). Judged by git — uncommitted or untracked changes, a stash, or a
#: commit not reachable from the clone's own upstream refs is use; a pristine
#: seed is not — and never weighed against ``empty_home_max_bytes`` (the aidream
#: clone is ~1.7 GB). Kept in step with sandbox-image by a guard test that reads
#: every entrypoint's seed target.
TEMPLATE_SEEDED_CLONES = frozenset({
    "aidream",  # entrypoint-aidream.sh: cp -a /opt/aidream-template → /home/agent/aidream
})

_last_sweep_at: float | None = None
#: Homes already proven to hold content, keyed by (volume, newest sandbox_id).
#: Content does not un-happen without a new row, so never re-inspect them.
_known_used: set[tuple[str, str]] = set()


def _classify_home(root, max_bytes):
    """Decide whether a mounted home holds user content. SELF-CONTAINED: this
    exact source is shipped into the helper container, so it may only use the
    standard library (and the ``git`` binary), imported here.

    Only FILES (and symlinks) are evidence: an empty directory never is, which
    is what the image and the aidream app leave behind (``workspace/``,
    ``temp/``, the layout dirs). A template-seeded clone
    (:data:`TEMPLATE_SEEDED_CLONES`) is judged by git, not by its files."""
    import os
    import subprocess

    content_dirs = (
        "projects/", "repos/", "scratch/",
        ".matrx/plans/", ".matrx/skills/", ".matrx/memory/",
        ".matrx/runtime/shell-logs/",
    )
    tool_calls = ".matrx/runtime/tool-calls/"
    tool_calls_service = ".matrx/runtime/tool-calls/_runtime/"
    histories = {".bash_history", ".python_history", ".zsh_history",
                 ".node_repl_history", ".sh_history"}
    layout_top = {"cloud-files", "repos", "projects", "scratch"}
    mirror = "cloud-files"
    template_clones = TEMPLATE_SEEDED_CLONES

    evidence = []
    unreadable = []
    total = 0
    files = 0

    def errored(exc):
        unreadable.append(str(getattr(exc, "filename", "") or exc))

    def git(repo, *args):
        # Read-only and inert: no optional index writes, no hooks, no
        # fsmonitor, no system/global config — the repo is the user's, the
        # helper is root, so nothing in it may run.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "GIT_OPTIONAL_LOCKS": "0",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "HOME": "/nonexistent", "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(
            ["git", "-c", "safe.directory=*", "-c", "core.fsmonitor=",
             "-c", "core.hooksPath=/dev/null", "-c", "core.untrackedCache=false",
             "-C", repo, *args],
            capture_output=True, text=True, env=env, timeout=600,
        )

    def clone_shows_work(name):
        """None when the clone is pristine; else a short reason."""
        repo = os.path.join(root, name)
        try:
            status = git(repo, "status", "--porcelain", "--untracked-files=normal")
            if status.returncode != 0:
                return "git status failed: " + status.stderr.strip()[:120]
            if status.stdout.strip():
                return "uncommitted: " + status.stdout.strip().splitlines()[0][:120]
            if git(repo, "rev-parse", "-q", "--verify", "refs/stash").returncode == 0:
                return "stash"
            # The baseline is what the clone itself knows came from upstream:
            # every remote-tracking ref, plus FETCH_HEAD (the template seed is
            # a shallow fetch of the release commit and has no remote refs).
            baseline = []
            try:
                with open(os.path.join(repo, ".git", "FETCH_HEAD")) as fh:
                    for line in fh:
                        sha = line[:40]
                        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
                            baseline.append(sha)
            except OSError:
                pass
            remotes = git(repo, "for-each-ref", "--format=%(objectname)", "refs/remotes")
            if remotes.returncode != 0:
                return "git for-each-ref failed"
            baseline += [s for s in remotes.stdout.split() if s]
            if not baseline:
                return "no upstream baseline to compare commits against"
            local = git(repo, "rev-list", "--max-count=1", "HEAD", "--branches", "--tags",
                        "--not", *baseline)
            if local.returncode != 0:
                return "git rev-list failed: " + local.stderr.strip()[:120]
            if local.stdout.strip():
                return "local commit " + local.stdout.strip()[:12]
            return None
        except Exception as exc:  # git missing, timeout, … = cannot prove pristine
            return "git unavailable: " + str(exc)[:120]

    try:
        top = os.listdir(root)
    except OSError as exc:
        return {"empty": False, "evidence": [], "unreadable": [str(exc)],
                "bytes": 0, "files": 0, "over_size": False}

    judged_clones = set()
    for name in top:
        if name in template_clones and os.path.isdir(os.path.join(root, name, ".git")) \
                and not os.path.islink(os.path.join(root, name)):
            judged_clones.add(name)
            why = clone_shows_work(name)
            if why:
                evidence.append(name + ": " + why)

    root_dev = os.lstat(root).st_dev
    for dirpath, dirnames, filenames in os.walk(root, onerror=errored, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        leaves = list(filenames)
        kept = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            rel = d if not rel_dir else rel_dir + "/" + d
            if not rel_dir and (d == mirror or d in judged_clones):
                continue  # mirror and git-judged clones: not walked, not weighed
            try:
                st = os.lstat(full)
            except OSError as exc:
                errored(exc)
                continue
            if os.path.islink(full):
                leaves.append(d)  # a symlink is an entry, never descended
            elif st.st_dev == root_dev:
                kept.append(d)
        dirnames[:] = kept
        for name in leaves:
            rel = name if not rel_dir else rel_dir + "/" + name
            first = rel.split("/", 1)[0]
            if first == mirror or first in judged_clones:
                continue
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError as exc:
                errored(exc)
                continue
            files += 1
            total += st.st_size
            if rel in histories:
                evidence.append(rel)
            elif rel.startswith(content_dirs):
                evidence.append(rel)
            elif rel.startswith(tool_calls) and not rel.startswith(tool_calls_service):
                evidence.append(rel)
            elif not first.startswith(".") and first not in layout_top:
                evidence.append(rel)
    over = total > int(max_bytes)
    return {
        "empty": not evidence and not unreadable and not over,
        "evidence": sorted(set(evidence))[:5],
        "unreadable": unreadable[:5],
        "bytes": total,
        "files": files,
        "over_size": over,
    }


def helper_program() -> str:
    """The python program the helper runs: :func:`_classify_home`, verbatim."""
    source = textwrap.dedent(inspect.getsource(_classify_home))
    return (
        f"TEMPLATE_SEEDED_CLONES = frozenset({sorted(TEMPLATE_SEEDED_CLONES)!r})\n"
        + source
        + "\nimport json, sys\n"
        + f"print({_RESULT_PREFIX!r} + json.dumps(_classify_home(sys.argv[1], sys.argv[2])))\n"
    )


async def inspect_home(client: Any, volume: str, max_bytes: int) -> dict:
    """Run :func:`_classify_home` against ``volume`` mounted READ-ONLY in a
    networkless helper. Raises on any failure (the caller keeps the home)."""
    from orchestrator.hosted_runtime import _helper_image

    image = await _helper_image(client)
    if not image:
        raise RuntimeError("no helper image identity")
    output = await asyncio.to_thread(
        client.containers.run,
        image,
        command=["-c", helper_program(), "/home/agent", str(int(max_bytes))],
        entrypoint=["python3"],
        user="0:0",
        volumes={volume: {"bind": "/home/agent", "mode": "ro"}},
        network_disabled=True,
        environment={},
        labels=_HELPER_LABELS,
        remove=True,
        detach=False,
    )
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output)
    lines = [ln for ln in text.splitlines() if ln.startswith(_RESULT_PREFIX)]
    if len(lines) != 1:
        raise RuntimeError("home inspector did not emit exactly one verdict")
    verdict = json.loads(lines[0][len(_RESULT_PREFIX):])
    if not isinstance(verdict, dict) or not isinstance(verdict.get("empty"), bool):
        raise RuntimeError("home inspector emitted a malformed verdict")
    return verdict


def _status(row: Any) -> str:
    return str(getattr(row.status, "value", row.status))


def _last_touch(rows: list[Any]) -> datetime:
    stamps = []
    for row in rows:
        for stamp in (row.created_at, row.stopped_at, row.last_heartbeat_at):
            if stamp is not None:
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                stamps.append(stamp)
    return max(stamps)


def qualifies(rows: list[Any], *, now: datetime, after_days: int) -> tuple[bool, str]:
    """The row half of the definition (conditions 1-4). Returns (ok, why)."""
    from orchestrator.always_on import is_always_on
    from orchestrator.storage_layout import user_volume_name

    if not rows:
        return False, "no rows"
    newest = max(rows, key=lambda r: r.created_at)
    cfg = newest.config or {}
    if isinstance(cfg, dict) and cfg.get(HOME_DELETED_CONFIG_KEY):
        return False, "already deleted"
    if any(str(getattr(r.tier, "value", r.tier) or "") != "hosted" for r in rows):
        return False, "not hosted"
    try:
        volume = user_volume_name(str(newest.user_id), str(newest.organization_id or ""))
    except ValueError:
        return False, "no canonical per-(user, organization) home"
    if any(r.persistence_volume and r.persistence_volume != volume for r in rows):
        return False, "rows name a different home"
    if any(_status(r) not in TERMINAL_STATUSES for r in rows):
        return False, "a box is up"
    if any(is_always_on(r.labels) for r in rows):
        return False, "always-on"
    idle = now - _last_touch(rows)
    if idle.total_seconds() < after_days * 86400:
        return False, "inside the period"
    return True, f"untouched {idle.days}d"


async def _read_knobs() -> tuple[int, int, int] | None:
    from orchestrator.knobs import knob_int
    try:
        return (await knob_int(AFTER_DAYS_KNOB), await knob_int(MAX_PER_PASS_KNOB),
                await knob_int(MAX_BYTES_KNOB))
    except Exception as exc:  # noqa: BLE001 — unreadable means OFF, loudly
        logger.warning(
            "Unused-home sweep OFF this pass: knobs unreadable (%s). Seed "
            "infrastructure.sandbox.%s / %s / %s to turn it on.",
            exc, AFTER_DAYS_KNOB, MAX_PER_PASS_KNOB, MAX_BYTES_KNOB,
        )
        return None


def _sweep_due() -> bool:
    global _last_sweep_at
    now = time.monotonic()
    if _last_sweep_at is not None and now - _last_sweep_at < SWEEP_INTERVAL_SECONDS:
        return False
    _last_sweep_at = now
    return True


async def sweep_unused_homes(store, *, force: bool = False) -> dict:
    """One pass. Never raises for one home; returns a summary."""
    from orchestrator.config import settings

    summary: dict[str, Any] = {"ran": False, "candidates": 0, "deleted": [],
                               "kept_used": 0, "failed": 0, "left_for_next_pass": 0}
    if settings.host_tier != "hosted":
        return summary
    if not force and not _sweep_due():
        return summary
    knobs = await _read_knobs()
    if knobs is None:
        return summary
    after_days, max_per_pass, max_bytes = knobs
    if after_days <= 0 or max_per_pass <= 0:
        return summary
    summary["ran"] = True

    pairs: dict[tuple[str, str], list[Any]] = {}
    for row in await store.list(include_deleted=True):
        if str(getattr(row.tier, "value", row.tier) or "") != "hosted":
            continue
        pairs.setdefault((str(row.user_id), str(row.organization_id or "")), []).append(row)

    now = datetime.now(timezone.utc)
    candidates = []
    for key, rows in pairs.items():
        try:
            ok, why = qualifies(rows, now=now, after_days=after_days)
        except Exception as exc:  # noqa: BLE001 — one odd row never ends the pass
            logger.warning("Unused-home sweep: (user=%s org=%s) skipped: %r", key[0], key[1], exc)
            continue
        if not ok:
            continue
        newest = max(rows, key=lambda r: r.created_at)
        from orchestrator.storage_layout import user_volume_name
        volume = user_volume_name(key[0], key[1])
        if (volume, newest.sandbox_id) in _known_used:
            continue
        candidates.append((_last_touch(rows), key, newest, why))
    candidates.sort(key=lambda c: c[0])  # longest-untouched first
    summary["candidates"] = len(candidates)
    if len(candidates) > max_per_pass:
        summary["left_for_next_pass"] = len(candidates) - max_per_pass
        logger.info("Unused-home sweep: %d candidate(s); inspecting %d (%s=%d).",
                    len(candidates), max_per_pass, MAX_PER_PASS_KNOB, max_per_pass)
        candidates = candidates[:max_per_pass]

    for _touch, key, newest, _why in candidates:
        try:
            outcome = await _delete_if_unused(store, key, newest.sandbox_id,
                                              after_days=after_days, max_bytes=max_bytes)
        except Exception as exc:  # noqa: BLE001 — one home never ends the pass
            summary["failed"] += 1
            logger.warning("Unused-home sweep: %s (user=%s org=%s) left alone: %r cause=%r",
                           newest.sandbox_id, key[0], key[1], exc, exc.__cause__)
            continue
        if outcome == "deleted":
            summary["deleted"].append(newest.sandbox_id)
        elif outcome == "used":
            summary["kept_used"] += 1
    return summary


async def _delete_if_unused(store, key: tuple[str, str], newest_id: str, *,
                            after_days: int, max_bytes: int) -> str:
    """Re-decide under the home's exclusive lifecycle lease, then delete."""
    from orchestrator.hosted_operation_lease import hosted_operation_lease
    from orchestrator import sandbox_manager
    from orchestrator.storage_layout import user_volume_name

    user_id, org_id = key
    volume = user_volume_name(user_id, org_id)
    async with hosted_operation_lease(f"volume-delete-{user_id}-{org_id}", volume,
                                      lifecycle=True):
        rows = [r for r in await store.list(user_id=user_id, include_deleted=True)
                if str(r.organization_id or "") == org_id
                and str(getattr(r.tier, "value", r.tier) or "") == "hosted"]
        ok, why = qualifies(rows, now=datetime.now(timezone.utc), after_days=after_days)
        newest = max(rows, key=lambda r: r.created_at) if rows else None
        if not ok or newest is None or newest.sandbox_id != newest_id:
            return "changed"

        client = sandbox_manager._get_docker_client()
        from docker.errors import NotFound
        try:
            await asyncio.to_thread(client.volumes.get, volume)
            present = True
        except NotFound:
            present = False
        verdict: dict = {"bytes": 0, "files": 0, "evidence": []}
        if present:
            attached = await asyncio.to_thread(
                client.containers.list, all=True, filters={"volume": volume})
            if attached:
                return "attached"
            verdict = await inspect_home(client, volume, max_bytes)
            if not verdict["empty"]:
                _known_used.add((volume, newest_id))
                logger.info(
                    "Unused-home sweep: %s (user=%s org=%s) holds user content, kept "
                    "(evidence=%s unreadable=%s bytes=%s).",
                    newest_id, user_id, org_id, verdict.get("evidence"),
                    verdict.get("unreadable"), verdict.get("bytes"),
                )
                return "used"
            if not await sandbox_manager.delete_user_volume(
                    user_id, org_id, _lifecycle_lease_held=True):
                raise RuntimeError(f"delete_user_volume refused {volume}")

        detail = {
            "reason": HOME_DELETED_REASON,
            "at": datetime.now(timezone.utc).isoformat(),
            "volume": volume,
            "volume_was_present": present,
            "why": why,
            "after_days": after_days,
            "home_bytes": verdict.get("bytes", 0),
            "home_files": verdict.get("files", 0),
        }
        await store.mark_home_deleted([r.sandbox_id for r in rows], detail)
    # WARNING: an automatic deletion of somebody's storage announces itself.
    logger.warning(
        "UNUSED HOME DELETED: sandbox=%s user=%s org=%s volume=%s — %s (>= %s=%dd), "
        "all rows terminal, not always-on, no user content (files=%s bytes=%s "
        "outside cloud-files)%s.",
        newest_id, user_id, org_id, volume, why, AFTER_DAYS_KNOB, after_days,
        verdict.get("files", 0), verdict.get("bytes", 0),
        "" if present else "; volume was already absent, rows marked only",
    )
    return "deleted"


__all__ = [
    "AFTER_DAYS_KNOB",
    "HOME_DELETED_CONFIG_KEY",
    "HOME_DELETED_REASON",
    "MAX_BYTES_KNOB",
    "MAX_PER_PASS_KNOB",
    "helper_program",
    "inspect_home",
    "qualifies",
    "sweep_unused_homes",
]
