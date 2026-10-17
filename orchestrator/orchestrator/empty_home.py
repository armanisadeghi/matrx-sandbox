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
   inside a networkless helper container (killed and removed after
   ``HELPER_TIMEOUT_SECONDS``; a timeout KEEPS the home). It is an ALLOW-LIST:
   every file or symlink anywhere in the home is content UNLESS the image's own
   never-used boot creates it — ``BOOT_SKELETON_EXACT`` / ``_PREFIXES``, the
   skel ``.profile``/``.bash_logout`` byte for byte, a ``.bashrc`` that is the
   skel plus only ``BASHRC_BOOT_LINES``, a ``.gitconfig`` holding only the
   credential keys boot writes, and a cloud-sync queue with nothing pending.
   Unknown is content (``~/.claude``, ``~/.ssh`` private keys, own-plan
   sign-ins, any other ``.matrx`` file). Empty directories never are.
   ``cloud-files/`` (a mirror of server-held files) is skipped. A
   TEMPLATE-SEEDED CLONE (``aidream/``) is judged by git: any changed,
   untracked or ignored path other than ``CLONE_BOOT_IGNORED`` (its baked
   ``.venv/``), a stash, or a commit not reachable from its own upstream refs
   is use. Over ``empty_home_max_bytes`` (mirror and clones excluded), or
   anything unreadable, is use.

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

#: One home inspection may take at most this long (a 1.7 GB template clone's
#: git status included). On expiry the helper is killed and removed and the
#: home is KEPT. Mechanism bounds, not opinions, so constants.
HELPER_TIMEOUT_SECONDS = 300
#: A whole pass runs in the background, never inside the 60 s reaper tick, and
#: is cancelled after this long (the lease is released on the way out).
SWEEP_PASS_BUDGET_SECONDS = 1800

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

#: Ignored paths a template's own boot creates inside its clone (``git status
#: --ignored=matching`` spelling). Any OTHER ignored path — a ``.env``, a data
#: dump — is the person's and makes the home used.
CLONE_BOOT_IGNORED = {
    "aidream": (".venv/",),  # cp -a of /opt/aidream-template carries the baked venv
}

# ── The never-used boot skeleton: the ONLY files that are not content ─────────
# Derived from sandbox-image (ensure-layout.sh, prepare-agent-home.sh, the
# entrypoints, configure-git-credentials.sh, the SDK's persistence + cloud_sync)
# and checked against the two homes the live host judged untouched on
# 2026-10-16. Anything not matched here is content. Guards:
# tests/test_unused_home_sweep.py.

#: REGULAR files boot (re)writes wholesale every start, so nothing a person put
#: in them survives a boot anyway. Exact paths only.
BOOT_SKELETON_EXACT = frozenset({
    ".sandbox_env",                           # entrypoint*.sh: cat > every boot
    ".matrx/instructions/SANDBOX_LAYOUT.md",  # ensure-layout.sh: cat > every boot
    ".matrx/session.json",                    # matrx_agent.persistence, every 5 min
    ".matrx/session.prev.json",               # matrx_agent.persistence, rotated at boot
    ".matrx/session-report.md",               # matrx_agent.persistence, rendered at boot
    ".matrx/runtime/cloud-files-down-complete",  # cloud_sync watcher marker (empty)
})

#: The one boot-created TREE, matched exactly: the Codex CLI's per-process
#: arg0 dispatch dir (seen on the live untouched aidream home 2026-10-16) — an
#: empty ``.lock`` and four symlinks, each to exactly :data:`CODEX_ARG0_TARGET`.
#: Anything else under ``.codex`` (or any other tree) is content.
CODEX_ARG0_DIR_RE = r"^\.codex/tmp/arg0/codex-arg0[A-Za-z0-9]+/"
CODEX_ARG0_LINKS = frozenset({"apply_patch", "applypatch", "codex-execve-wrapper",
                              "codex-linux-sandbox"})
CODEX_ARG0_TARGET = ("/opt/matrx-codex/lib/node_modules/@openai/codex/node_modules/"
                     "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex")

#: Ubuntu 22.04 /etc/skel copies, byte for byte (measured from the live
#: matrx-sandbox:core image 2026-10-16). An edited copy is content.
SKEL_SHA256 = {
    ".profile": "28b4a453b68dde64f814e94bab14ee651f4f162e15dd9920490aa1d49f05d2a4",
    ".bash_logout": "26882b79471c25f945c970f8233d8ce29d54e9d5eedcd2884f88affa84a18f56",
    # Baked at image build (Dockerfile*: cp admin_authorized_keys), identical in
    # core/slim/aidream and both untouched homes; = sandbox-image/config/
    # admin_authorized_keys. prepare-agent-home.sh only appends keys from that
    # same file, so any other line is somebody's key.
    ".ssh/authorized_keys": "c041196992f03beea13b1cff1815e3d47f48548cf02842ef3b29bbea453688bd",
}
SKEL_BASHRC_SHA256 = "342099da4dd28c394d3f8782d90d7465cb2eaa611193f8f378d6918261cb9bb8"

#: Lines boot appends to the skel .bashrc. With these removed the file must be
#: the skel byte for byte; any other line is the person's.
BASHRC_BOOT_LINES = frozenset({
    "[ -f ~/.sandbox_env ] && source ~/.sandbox_env",  # entrypoint*.sh
    ". /etc/profile.d/aidream.sh",                       # Dockerfile.aidream
    "# SANDBOX_PROMPT",                                  # entrypoint-local.sh block
    'export PS1="\\[\\033[1;36m\\]sandbox:${SANDBOX_ID:-?}\\[\\033[0m\\] '
    '\\[\\033[1;34m\\]\\w\\[\\033[0m\\] \\$ "',
    "alias ll='ls -alh --color=auto'",
    "alias la='ls -A --color=auto'",
})

#: configure-git-credentials.sh writes credential.helper (exactly these two,
#: its defaults) and the github.com username (rewritten every boot); any other
#: .gitconfig key or helper value is the person's.
GITCONFIG_BOOT_HELPERS = frozenset({
    "cache --socket=/home/agent/.matrx/runtime/git-credential-cache.sock --timeout=1800",
    "/opt/sandbox/scripts/matrx-git-credential-env",
})

#: The boot copy (``cp -a`` + ``chown -R`` + editable retarget) stamps every
#: ``.venv`` entry's ctime within one window; ``cp -a`` cannot carry ctime, so
#: an entry changed AFTER the copy (a pip install, an edit) has a later ctime.
#: Measured from ``.venv/pyvenv.cfg``. Byte-code caches are exempt.
VENV_COPY_WINDOW_SECONDS = 3600

#: Every git call in the helper is capped; a timeout means "cannot prove
#: pristine", i.e. KEEP.
GIT_CALL_TIMEOUT_SECONDS = 60

#: The constants :func:`_classify_home` reads, shipped into the helper.
_SHIPPED_CONSTANTS = (
    "TEMPLATE_SEEDED_CLONES", "CLONE_BOOT_IGNORED", "BOOT_SKELETON_EXACT",
    "CODEX_ARG0_DIR_RE", "CODEX_ARG0_LINKS", "CODEX_ARG0_TARGET",
    "SKEL_SHA256", "SKEL_BASHRC_SHA256", "BASHRC_BOOT_LINES",
    "GITCONFIG_BOOT_HELPERS", "GIT_CALL_TIMEOUT_SECONDS", "VENV_COPY_WINDOW_SECONDS",
)

_last_sweep_at: float | None = None
#: Homes already proven to hold content, keyed by (volume, newest sandbox_id).
#: Content does not un-happen without a new row, so never re-inspect them.
_known_used: set[tuple[str, str]] = set()


def _classify_home(root, max_bytes, release_shas=()):
    """Decide whether a mounted home holds user content. ALLOW-LIST: every
    file or symlink anywhere in the home is content unless the image's own
    never-used boot is known to create it (:data:`BOOT_SKELETON_EXACT`,
    the exact Codex arg0 links, the verified dotfiles below). Unknown is
    content. Empty directories are never content. ``cloud-files/`` (a mirror)
    is skipped; a template-seeded clone is judged by git.

    SELF-CONTAINED: this exact source, plus the module constants it names,
    is shipped into the helper container; standard library + ``git`` only."""
    import hashlib
    import json
    import os
    import re
    import stat as statmod
    import subprocess

    mirror = "cloud-files"
    evidence = []
    unreadable = []
    total = 0
    files = 0

    def errored(exc):
        unreadable.append(str(getattr(exc, "filename", "") or exc))

    def git(repo, *args):
        # Read-only and inert: no optional index writes, no hooks, no
        # fsmonitor, no system/global config — the repo is the user's, the
        # helper is root, so nothing in it may run. Bounded per call.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "GIT_OPTIONAL_LOCKS": "0",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "HOME": "/nonexistent", "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(
            ["git", "-c", "safe.directory=*", "-c", "core.fsmonitor=",
             "-c", "core.hooksPath=/dev/null", "-c", "core.untrackedCache=false",
             *(["-C", repo] if repo else []), *args],
            capture_output=True, text=True, env=env, timeout=GIT_CALL_TIMEOUT_SECONDS,
        )

    def sha(path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def boot_file_ok(rel, full, st):
        """True only when a known boot-written file holds only what boot wrote.
        A symlink is content unless it is one of the exact Codex arg0 links."""
        codex = re.match(CODEX_ARG0_DIR_RE, rel)
        if codex:
            leaf = rel[codex.end():]
            if statmod.S_ISLNK(st.st_mode):
                return leaf in CODEX_ARG0_LINKS and os.readlink(full) == CODEX_ARG0_TARGET
            return leaf == ".lock" and statmod.S_ISREG(st.st_mode) and st.st_size == 0
        if statmod.S_ISLNK(st.st_mode):
            return False
        if rel in BOOT_SKELETON_EXACT:
            return True
        if rel in SKEL_SHA256:
            return sha(full) == SKEL_SHA256[rel]
        if rel == ".bashrc":
            with open(full, "rb") as fh:
                lines = fh.read().decode("utf-8", "replace").splitlines(keepends=True)
            kept = "".join(ln for ln in lines if ln.rstrip("\n") not in BASHRC_BOOT_LINES)
            return hashlib.sha256(kept.encode()).hexdigest() == SKEL_BASHRC_SHA256
        if rel == ".gitconfig":
            listed = git(None, "config", "--file", full, "--list")
            if listed.returncode != 0:
                return False
            for entry in listed.stdout.splitlines():
                key, _, value = entry.partition("=")
                if key == "credential.https://github.com.username":
                    continue
                if key == "credential.helper" and value in GITCONFIG_BOOT_HELPERS:
                    continue
                return False
            return True
        if rel == ".matrx/runtime/cloud-sync-queue.jsonl":
            # Allowed only when every queued upload is DONE — a pending one is a
            # file that has not reached the person's AI Matrx files yet.
            pending = set()
            with open(full, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    eid = obj.get("event_id")
                    if obj.get("done"):
                        pending.discard(eid)
                    else:
                        pending.add(eid)
            return not pending
        return False

    def venv_changed_after_copy(venv):
        """None when every .venv entry was stamped by the boot copy."""
        try:
            anchor = os.lstat(os.path.join(venv, "pyvenv.cfg")).st_ctime
        except OSError:
            return ".venv has no pyvenv.cfg"
        limit = anchor + VENV_COPY_WINDOW_SECONDS
        for dirpath, dirnames, filenames in os.walk(venv, followlinks=False):
            if os.path.basename(dirpath) == "__pycache__":
                dirnames[:] = []
                continue
            # Files and symlinks only: a directory's ctime moves whenever an
            # entry (a byte-code cache) is added inside it.
            for name in filenames + [d for d in dirnames
                                     if os.path.islink(os.path.join(dirpath, d))]:
                full = os.path.join(dirpath, name)
                if os.lstat(full).st_ctime > limit:
                    return ".venv changed after its boot copy: " + os.path.relpath(full, venv)[:120]
        return None

    def clone_shows_work(name):
        """None when the clone is pristine; else a short reason."""
        repo = os.path.join(root, name)
        try:
            status = git(repo, "status", "--porcelain", "--untracked-files=normal",
                         "--ignored=matching")
            if status.returncode != 0:
                return "git status failed: " + status.stderr.strip()[:120]
            allowed_ignored = set(CLONE_BOOT_IGNORED.get(name, ()))
            for line in status.stdout.splitlines():
                if line.startswith("!! ") and line[3:] in allowed_ignored:
                    continue
                return "changed: " + line[:120]
            if git(repo, "rev-parse", "-q", "--verify", "refs/stash").returncode == 0:
                return "stash"
            # Hidden from `git status` on purpose: assume-unchanged (lowercase
            # tag) or skip-worktree (S) entries can carry edits status won't show.
            flagged = git(repo, "ls-files", "-v")
            if flagged.returncode != 0:
                return "git ls-files failed"
            for line in flagged.stdout.splitlines():
                if line[:1].islower() or line[:1] == "S":
                    return "hidden from status: " + line[:120]
            # The baseline is upstream as the clone records it (refs/remotes/*)
            # plus the release commits of the aidream images on this host (the
            # seed is a shallow fetch of exactly that commit). FETCH_HEAD is
            # NOT a baseline: any local fetch rewrites it.
            remotes = git(repo, "for-each-ref", "--format=%(objectname)", "refs/remotes")
            if remotes.returncode != 0:
                return "git for-each-ref failed"
            baseline = [s for s in remotes.stdout.split() if s]
            for sha_ in release_shas:
                if git(repo, "cat-file", "-e", sha_ + "^{commit}").returncode == 0:
                    baseline.append(sha_)
            if not baseline:
                return "no upstream baseline to compare commits against"
            local = git(repo, "rev-list", "--max-count=1", "HEAD", "--branches", "--tags",
                        "--not", *baseline)
            if local.returncode != 0:
                return "git rev-list failed: " + local.stderr.strip()[:120]
            if local.stdout.strip():
                return "local commit " + local.stdout.strip()[:12]
            if ".venv/" in allowed_ignored and os.path.isdir(os.path.join(repo, ".venv")):
                why = venv_changed_after_copy(os.path.join(repo, ".venv"))
                if why:
                    return why
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
        if name in TEMPLATE_SEEDED_CLONES and os.path.isdir(os.path.join(root, name, ".git")) \
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
            else:
                evidence.append((d if not rel_dir else rel_dir + "/" + d) + " (other filesystem)")
        dirnames[:] = kept
        for name in leaves:
            rel = name if not rel_dir else rel_dir + "/" + name
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError as exc:
                errored(exc)
                continue
            if not (statmod.S_ISREG(st.st_mode) or statmod.S_ISLNK(st.st_mode)):
                continue  # sockets, fifos: runtime plumbing, never data
            files += 1
            total += st.st_size
            try:
                ok = boot_file_ok(rel, full, st)
            except Exception as exc:  # unreadable or malformed = content
                errored(exc)
                ok = False
            if not ok:
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
    """The python program the helper runs: :func:`_classify_home`, verbatim,
    preceded by the exact values of the constants it reads."""
    prelude = "".join(f"{name} = {globals()[name]!r}\n" for name in _SHIPPED_CONSTANTS)
    source = textwrap.dedent(inspect.getsource(_classify_home))
    return (
        prelude
        + source
        + "\nimport json, sys\n"
        + "_release = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []\n"
        + f"print({_RESULT_PREFIX!r} + json.dumps(_classify_home(sys.argv[1], sys.argv[2], _release)))\n"
    )


def aidream_release_shas(client: Any) -> list[str]:
    """Release commits of every sandbox image on this host (the label
    ``com.aimatrx.aidream.sha`` Dockerfile.aidream stamps). A seeded clone's
    HEAD is one of these; nothing else about the clone is trusted as upstream."""
    shas: set[str] = set()
    for image in client.images.list():
        sha_ = ((getattr(image, "labels", None) or {}).get("com.aimatrx.aidream.sha") or "")
        if len(sha_) == 40 and all(c in "0123456789abcdef" for c in sha_):
            shas.add(sha_)
    return sorted(shas)


def _run_helper_bounded(client: Any, image: str, volume: str, max_bytes: int,
                        timeout: float, release_shas: list[str] | None = None) -> str:
    """Run the helper detached, wait at most ``timeout`` seconds, and ALWAYS
    remove it. Runs whole in one worker thread, so a cancelled caller can never
    skip the kill/remove. A timeout raises: the caller keeps the home."""
    container = client.containers.run(
        image,
        command=["-c", helper_program(), "/home/agent", str(int(max_bytes)),
                 json.dumps(list(release_shas or []))],
        entrypoint=["python3"],
        user="0:0",
        volumes={volume: {"bind": "/home/agent", "mode": "ro"}},
        network_disabled=True,
        environment={},
        labels=_HELPER_LABELS,
        detach=True,
    )
    try:
        try:
            result = container.wait(timeout=timeout)
        except Exception as exc:  # requests ReadTimeout / ConnectionError
            try:
                container.kill()
            except Exception:  # noqa: BLE001 — already gone is fine
                pass
            raise TimeoutError(f"home inspector exceeded {timeout:.0f}s ({exc!r})") from exc
        output = container.logs(stdout=True, stderr=False)
        code = (result or {}).get("StatusCode")
        if code != 0:
            raise RuntimeError(f"home inspector exited {code}")
        return output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output)
    finally:
        try:
            container.remove(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unused-home sweep: could not remove inspector container: %r", exc)


async def inspect_home(client: Any, volume: str, max_bytes: int) -> dict:
    """Run :func:`_classify_home` against ``volume`` mounted READ-ONLY in a
    networkless helper, bounded by :data:`HELPER_TIMEOUT_SECONDS`. Raises on
    any failure or timeout (the caller keeps the home)."""
    from orchestrator.hosted_runtime import _helper_image

    image = await _helper_image(client)
    if not image:
        raise RuntimeError("no helper image identity")
    shas = await asyncio.to_thread(aidream_release_shas, client)
    text = await asyncio.to_thread(_run_helper_bounded, client, image, volume, max_bytes,
                                   HELPER_TIMEOUT_SECONDS, shas)
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


_running: "asyncio.Task | None" = None


def launch_sweep(store) -> bool:
    """Start one pass in the background unless one is still running. The
    reaper tick calls this and never waits: an inspection that hangs can never
    stall expiry, liveness or always-on revive. Returns True when started."""
    global _running
    if _running is not None and not _running.done():
        return False
    if not _sweep_due():
        return False

    async def _bounded() -> None:
        try:
            await asyncio.wait_for(sweep_unused_homes(store, force=True),
                                   timeout=SWEEP_PASS_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Unused-home sweep: pass exceeded %ds and was cancelled; "
                           "nothing further deleted this pass.", SWEEP_PASS_BUDGET_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unused-home sweep: pass failed: %r", exc)

    _running = asyncio.get_running_loop().create_task(_bounded(), name="unused-home-sweep")
    return True


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
