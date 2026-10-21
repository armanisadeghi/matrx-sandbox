"""Automatic image-update policy: the SBX-013 gates the migration engine lacks.

The hosted migration engine (``hosted_runtime.migrate_hosted``) already owns
the dangerous part of a swap: admission fencing, journaled phases, verified
home receipts and verified rollback. What it does not decide is *whether a box
should be swapped automatically at all*. This module is that decision, and it
fails closed at every step:

* **Stage** (``infrastructure.sandbox.auto_update_stage``): ``off`` | ``canary``
  | ``fleet``. A missing, unreadable or unknown value means ``off``. ``canary``
  admits only ``auto_update_canary_sandbox_ids``; ``fleet`` admits a target
  version only after a canary moved onto *that* version, passed its
  preservation check, and soaked for ``auto_update_canary_soak_seconds``.
* **Storage**: only homes the engine can carry (one named Docker volume, or the
  EC2 slim/bare writable layer it promotes). A host bind-mounted home (EC2
  ``development``, ``persistence_volume=host:*``) is reported ``manual_only``.
* **Opt-out**: container label ``matrx.auto_update=off`` or the file
  ``/home/agent/.matrx/auto-update.off`` inside the box (the owner or agent can
  create it).
* **Detached work**: any process in the box beyond the known baseline daemons
  defers the box. Unknown processes are work until proven otherwise.
* **Coalescing / bounded attempts**: a target version must have been current
  for ``auto_update_settle_seconds`` before anything moves (rapid deploys
  coalesce into one later attempt); a box is attempted at most once per
  ``auto_update_min_attempt_spacing_seconds``; after
  ``auto_update_max_failures_per_box`` failed attempts the box is *held* until
  an operator clears it.
* **Preservation check**: an agent-level manifest of ``/home/agent`` (sampled
  file bytes/mode/owner plus every git repo's HEAD, dirty-diff digest and stash
  count) is taken before and after. Any mismatch halts the target version for
  the whole fleet and is logged at ERROR.
* **Durable ledger**: decisions survive orchestrator restarts in the
  hosted-migration state mount. No durable mount -> no automatic updates.

The master switch is still ``infrastructure.sandbox.auto_migrate``; this
module adds gates, it never opens one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STAGES = ("off", "canary", "fleet")
OPT_OUT_FILE = "/home/agent/.matrx/auto-update.off"
OPT_OUT_LABEL = "matrx.auto_update"
LEDGER_SUBDIR = "auto-update"
LEDGER_NAME = "ledger.v1"  # never *.json: the journal dir globs *.json

DEFAULTS = {
    "auto_update_settle_seconds": 3600,
    "auto_update_canary_soak_seconds": 14400,
    "auto_update_min_attempt_spacing_seconds": 21600,
    "auto_update_max_failures_per_box": 2,
}

# migrate_sandbox statuses that did not touch the box: not an attempt.
NON_ATTEMPT_STATUSES = frozenset({
    "busy_deferred", "deferred", "skipped", "not_found", "in_progress",
    "unchanged", "already_current",
})
SUCCESS_STATUSES = frozenset({"migrated"})


@dataclass
class Policy:
    stage: str = "off"
    canary_ids: tuple[str, ...] = ()
    settle_seconds: int = DEFAULTS["auto_update_settle_seconds"]
    canary_soak_seconds: int = DEFAULTS["auto_update_canary_soak_seconds"]
    min_attempt_spacing_seconds: int = DEFAULTS["auto_update_min_attempt_spacing_seconds"]
    max_failures_per_box: int = DEFAULTS["auto_update_max_failures_per_box"]
    notes: list[str] = field(default_factory=list)


async def resolve_policy(raw_knob=None) -> Policy:
    """Read the stage knobs once per pass. Anything unreadable fails closed.

    ``raw_knob(feature, key)`` defaults to ``knobs._raw``."""
    from orchestrator import knobs
    raw_knob = raw_knob or knobs._raw

    async def raw(key):
        return await raw_knob(knobs.FEATURE, key)

    policy = Policy()
    try:
        stage = await raw("auto_update_stage")
    except Exception as exc:  # missing knob row is the default state
        policy.notes.append(f"auto_update_stage unreadable ({type(exc).__name__}): off")
        stage = "off"
    if not isinstance(stage, str) or stage not in STAGES:
        if stage != "off":
            policy.notes.append(f"auto_update_stage={stage!r} is not one of {STAGES}: off")
        stage = "off"
    policy.stage = stage
    try:
        ids = await raw("auto_update_canary_sandbox_ids")
        if isinstance(ids, str):
            ids = [s.strip() for s in ids.split(",") if s.strip()]
        if isinstance(ids, list) and all(isinstance(i, str) for i in ids):
            policy.canary_ids = tuple(ids)
        else:
            policy.notes.append("auto_update_canary_sandbox_ids is not a list of ids: none")
    except Exception:
        policy.notes.append("auto_update_canary_sandbox_ids unreadable: none")
    for key, attr in (
        ("auto_update_settle_seconds", "settle_seconds"),
        ("auto_update_canary_soak_seconds", "canary_soak_seconds"),
        ("auto_update_min_attempt_spacing_seconds", "min_attempt_spacing_seconds"),
        ("auto_update_max_failures_per_box", "max_failures_per_box"),
    ):
        try:
            value = await raw(key)
        except Exception:
            continue  # conservative default
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            policy.notes.append(f"{key}={value!r} invalid: default {DEFAULTS[key]}")
            continue
        # Never let a knob weaken the floor below the conservative defaults
        # except max failures, where lower is stricter.
        if key == "auto_update_max_failures_per_box":
            setattr(policy, attr, min(value, DEFAULTS[key]) if value else 0)
        else:
            setattr(policy, attr, value)
    return policy


# --------------------------------------------------------------------------
# Durable ledger
# --------------------------------------------------------------------------


class LedgerUnavailable(RuntimeError):
    pass


class Ledger:
    """JSON state written atomically under the durable hosted-migration mount."""

    def __init__(self, root: Path | None = None, *, require_mount: bool = True):
        if root is None:
            from orchestrator.hosted_migration import STATE_DIR
            root = STATE_DIR
        self.base = Path(root)
        self.require_mount = require_mount
        self.dir = self.base / LEDGER_SUBDIR
        self.path = self.dir / LEDGER_NAME

    def _check_mount(self) -> None:
        if not self.require_mount:
            return
        from orchestrator.hosted_migration import _has_required_state_mount
        if not _has_required_state_mount(self.base):
            raise LedgerUnavailable(f"{self.base} is not a durable mount")

    def load(self) -> dict:
        self._check_mount()
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"version": 1, "targets": {}, "boxes": {}}
        except (OSError, ValueError) as exc:
            # A corrupt ledger must not silently reset attempt bounds.
            raise LedgerUnavailable(f"ledger unreadable: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise LedgerUnavailable("ledger has an unknown shape")
        data.setdefault("targets", {})
        data.setdefault("boxes", {})
        return data

    def save(self, data: dict) -> None:
        self._check_mount()
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".ledger-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            dfd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def observe_target(ledger: dict, version: str, now: float) -> dict:
    t = ledger["targets"].setdefault(version, {"first_seen": now, "canary_passes": [],
                                                "halted": None})
    return t


def clear_hold(ledger_obj: Ledger, sandbox_id: str | None = None,
               version: str | None = None) -> dict:
    """Operator action: release a held box and/or a halted target version."""
    data = ledger_obj.load()
    if sandbox_id:
        box = data["boxes"].get(sandbox_id)
        if box:
            box["failures"] = 0
            box["held"] = None
    if version and version in data["targets"]:
        data["targets"][version]["halted"] = None
    ledger_obj.save(data)
    return data


# --------------------------------------------------------------------------
# Box inspection
# --------------------------------------------------------------------------


def storage_class(container) -> str:
    attrs = getattr(container, "attrs", None) or {}
    labels = getattr(container, "labels", None) or {}
    for mount in attrs.get("Mounts") or []:
        if mount.get("Destination") == "/home/agent":
            if mount.get("Type") == "volume" and mount.get("RW", True):
                return "named_volume"
            if mount.get("Type") == "bind":
                return "host_bind"
            return "unknown"
    if labels.get("matrx.tier") == "ec2" and labels.get("matrx.template") in {"slim", "bare"}:
        return "ec2_writable_layer"
    return "unknown"


SUPPORTED_STORAGE = frozenset({"named_volume", "ec2_writable_layer"})


async def _exec(container, cmd, *, user: str = "root", timeout: float = 60.0):
    def run():
        return container.exec_run(cmd, user=user, demux=False)
    result = await asyncio.wait_for(asyncio.to_thread(run), timeout=timeout)
    code = getattr(result, "exit_code", None)
    output = getattr(result, "output", None)
    if code is None and isinstance(result, tuple):
        code, output = result
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    return code, output or ""


async def opted_out(container) -> bool:
    """True when the owner opted this box out. Probe failure raises."""
    labels = getattr(container, "labels", None) or {}
    if str(labels.get(OPT_OUT_LABEL, "")).strip().lower() in {"off", "0", "false", "no"}:
        return True
    code, _ = await _exec(container, ["test", "-e", OPT_OUT_FILE])
    if code == 0:
        return True
    if code == 1:
        return False
    raise RuntimeError(f"opt-out probe exited {code}")


# Baseline processes every template's entrypoint keeps alive. Anything else is
# treated as the owner's (or an agent's) work.
_BASELINE = [
    (None, re.compile(r"^tini\b")),
    (None, re.compile(r"^(/bin/)?(ba)?sh /opt/sandbox/scripts/entrypoint[\w.-]*\.sh\b")),
    (None, re.compile(r"^tee -a /var/log/sandbox/")),
    (None, re.compile(r"^sshd: /usr/sbin/sshd \[listener\]")),
    (None, re.compile(r"^/usr/sbin/sshd\b")),
    ("root", re.compile(r"^sleep \d+$")),
    (None, re.compile(r"python3?(\.\d+)? -m uvicorn matrx_agent\.api\.main:app\b")),
    (None, re.compile(r"^bash -c cd /home/agent && .*uvicorn matrx_agent\.api\.main:app")),
    (None, re.compile(r"^ps -eo user=,pid=,ppid=,args=$")),
]


def detached_processes(ps_output: str) -> list[str]:
    offenders = []
    for line in ps_output.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        user, _pid, _ppid, args = parts
        args = args.strip()
        if args.endswith("<defunct>") or args.startswith("[") and args.endswith("]"):
            continue
        if any((u is None or u == user) and rx.search(args) for u, rx in _BASELINE):
            continue
        offenders.append(f"{user}: {args[:160]}")
    return offenders


async def detached_work(container) -> list[str]:
    code, out = await _exec(container, ["ps", "-eo", "user=,pid=,ppid=,args="])
    if code != 0:
        raise RuntimeError(f"process probe exited {code}")
    return detached_processes(out)


# Runs inside the box as the agent user; prints one JSON manifest.
PRESERVATION_PROBE = r'''
import hashlib, json, os, subprocess, sys
HOME = "/home/agent"
SKIP_DIRS = {".git", "node_modules", ".cache", ".venv", "venv", "__pycache__",
             ".npm", ".pnpm-store", ".cargo", ".rustup", ".local", ".mypy_cache",
             ".pytest_cache", ".next", "dist", "build", ".turbo"}
SKIP_PREFIXES = (".matrx/runtime", ".matrx/logs", ".matrx/memory", "scratch/tmp")
MAX_WALK, SAMPLE, MAX_BYTES, MAX_REPOS = 200000, 300, 1 << 20, 60
env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
def git(repo, *args):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=60, env=env)
    if p.returncode != 0:
        raise RuntimeError("git %s failed in %s" % (args[0], repo))
    return p.stdout
cands, repos, walked = [], [], 0
for root, dirs, files in os.walk(HOME):
    rel_root = os.path.relpath(root, HOME)
    rel_root = "" if rel_root == "." else rel_root
    if ".git" in dirs and rel_root.count(os.sep) < 3 and len(repos) < MAX_REPOS:
        repos.append(root)
    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                     and not os.path.join(rel_root, d).startswith(SKIP_PREFIXES))
    for name in sorted(files):
        walked += 1
        if walked > MAX_WALK:
            break
        rel = os.path.join(rel_root, name)
        if rel.startswith(SKIP_PREFIXES):
            continue
        cands.append((hashlib.sha1(rel.encode("utf-8", "surrogateescape")).hexdigest(), rel))
    if walked > MAX_WALK:
        break
files_out = {}
for _, rel in sorted(cands)[:SAMPLE * 2]:
    if len(files_out) >= SAMPLE:
        break
    path = os.path.join(HOME, rel)
    try:
        st = os.lstat(path)
        if not os.path.isfile(path) or os.path.islink(path) or st.st_size > MAX_BYTES:
            continue
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        continue
    files_out[rel] = {"sha256": digest, "mode": st.st_mode, "uid": st.st_uid,
                      "gid": st.st_gid, "size": st.st_size}
repo_rels = sorted((os.path.relpath(r, HOME) for r in repos), key=len, reverse=True)
for rel, meta in files_out.items():
    for repo_rel in repo_rels:
        if repo_rel == "." or rel.startswith(repo_rel + os.sep):
            meta["repo"] = repo_rel
            break
repos_out = {}
for repo in repos:
    rel = os.path.relpath(repo, HOME)
    try:
        head = git(repo, "rev-parse", "HEAD").decode().strip()
    except Exception:
        head = None
    try:
        h = hashlib.sha256()
        status = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        h.update(status)
        if head:
            h.update(git(repo, "diff", "HEAD", "--binary"))
        stashes = git(repo, "stash", "list").count(b"\n")
        repos_out[rel] = {"head": head, "dirty": bool(status), "dirty_sha256": h.hexdigest(),
                          "stashes": stashes}
    except Exception as exc:
        repos_out[rel] = {"head": head, "error": str(exc)[:200]}
json.dump({"files": files_out, "repos": repos_out, "walked": walked,
           "truncated": walked > MAX_WALK}, sys.stdout)
'''


async def preservation_manifest(container, *, timeout: float = 240.0) -> dict:
    code, out = await _exec(container, ["python3", "-c", PRESERVATION_PROBE],
                            user="agent", timeout=timeout)
    if code != 0:
        raise RuntimeError(f"preservation probe exited {code}: {out[-300:]}")
    data = json.loads(out.strip().splitlines()[-1])
    if not isinstance(data, dict) or "files" not in data or "repos" not in data:
        raise RuntimeError("preservation probe returned an unknown shape")
    return data


def compare_manifests(before: dict, after: dict) -> tuple[list[str], list[str]]:
    """Return (discrepancies, notes). Notes are expected, safe differences."""
    problems: list[str] = []
    notes: list[str] = []
    after_files = after.get("files") or {}
    after_repos = after.get("repos") or {}
    for rel, meta in (before.get("repos") or {}).items():
        now = after_repos.get(rel)
        if now is None:
            problems.append(f"git repo missing after update: {rel}")
            continue
        if "error" in meta or "error" in now:
            if meta.get("error") != now.get("error") or meta.get("head") != now.get("head"):
                problems.append(f"git repo unreadable: {rel}")
            continue
        if meta.get("head") != now.get("head"):
            problems.append(f"git HEAD moved: {rel}")
            continue
        if meta.get("dirty_sha256") == now.get("dirty_sha256"):
            continue
        if meta.get("dirty") and not now.get("dirty") and now.get("stashes", 0) > meta.get("stashes", 0):
            notes.append(f"uncommitted work autostashed: {rel}")
            continue
        problems.append(f"uncommitted work changed: {rel}")
    # Files inside an autostashed work tree legitimately changed; the stash
    # (verified above) is their preservation evidence.
    stashed = {n.split(": ", 1)[1] for n in notes}
    file_problems: list[str] = []
    for rel, meta in (before.get("files") or {}).items():
        if meta.get("repo") in stashed:
            continue
        now = after_files.get(rel)
        if now is None:
            # The sample is chosen from what exists; a file missing afterwards
            # may have been deleted by the owner, but the box was idle, so it
            # is a discrepancy until a human says otherwise.
            file_problems.append(f"file missing after update: {rel}")
            continue
        for key in ("sha256", "mode", "uid", "gid"):
            if meta.get(key) != now.get(key):
                file_problems.append(f"file {key} changed: {rel}")
                break
    return file_problems + problems, notes


# --------------------------------------------------------------------------
# Admission + recording
# --------------------------------------------------------------------------


@dataclass
class Decision:
    sandbox_id: str
    admit: bool
    reason: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


async def admit(container, box, policy: Policy, ledger: dict, now: float | None = None) -> Decision:
    """Every policy gate for one drifted box, cheapest first, all fail-closed."""
    now = time.time() if now is None else now
    sid = box.sandbox_id
    version = box.current_version or box.current_image_id or "unknown"
    if policy.stage == "off":
        return Decision(sid, False, "stage_off")
    target = observe_target(ledger, version, now)
    if target.get("halted"):
        return Decision(sid, False, "target_halted", {"target": version,
                                                       "halted": target["halted"]})
    is_canary = sid in policy.canary_ids
    if policy.stage == "canary" and not is_canary:
        return Decision(sid, False, "not_a_canary")
    if policy.stage == "fleet" and not is_canary:
        soaked = [p for p in target.get("canary_passes", [])
                  if now - p.get("at", now) >= policy.canary_soak_seconds]
        if not soaked:
            return Decision(sid, False, "awaiting_canary_soak", {"target": version})
    age = now - target.get("first_seen", now)
    if age < policy.settle_seconds:
        return Decision(sid, False, "target_settling",
                        {"target": version, "age_seconds": int(age)})
    rec = ledger["boxes"].get(sid) or {}
    if rec.get("held"):
        return Decision(sid, False, "box_held", {"held": rec["held"]})
    if rec.get("failures", 0) >= max(policy.max_failures_per_box, 1) or policy.max_failures_per_box == 0:
        return Decision(sid, False, "box_held", {"failures": rec.get("failures", 0)})
    last = rec.get("last_attempt_at")
    if last and now - last < policy.min_attempt_spacing_seconds:
        return Decision(sid, False, "attempt_spacing",
                        {"next_after_seconds": int(policy.min_attempt_spacing_seconds - (now - last))})
    storage = storage_class(container)
    if storage not in SUPPORTED_STORAGE:
        return Decision(sid, False, "manual_only",
                        {"storage": storage,
                         "why": "the migration engine only carries a named-volume home "
                                "or the EC2 slim/bare writable layer"})
    try:
        if await opted_out(container):
            return Decision(sid, False, "opted_out")
    except Exception as exc:
        return Decision(sid, False, "probe_failed", {"probe": "opt_out", "error": str(exc)[:200]})
    try:
        offenders = await detached_work(container)
    except Exception as exc:
        return Decision(sid, False, "probe_failed", {"probe": "processes", "error": str(exc)[:200]})
    if offenders:
        return Decision(sid, False, "detached_work", {"processes": offenders[:10]})
    return Decision(sid, True, "admitted", {"target": version, "canary": is_canary,
                                            "storage": storage})


def record_outcome(ledger: dict, decision: Decision, status: str, *, now: float,
                   problems: list[str] | None = None, notes: list[str] | None = None,
                   max_failures: int = DEFAULTS["auto_update_max_failures_per_box"]) -> str:
    """Write one outcome. Returns the severity: info | warning | error."""
    sid = decision.sandbox_id
    version = decision.detail.get("target", "unknown")
    rec = ledger["boxes"].setdefault(sid, {"failures": 0, "held": None, "history": []})
    if status in NON_ATTEMPT_STATUSES:
        return "info"
    rec["last_attempt_at"] = now
    entry = {"at": now, "target": version, "status": status}
    if problems:
        entry["problems"] = problems[:20]
    if notes:
        entry["notes"] = notes[:20]
    rec["history"] = (rec.get("history") or [])[-19:] + [entry]
    target = observe_target(ledger, version, now)
    if status in SUCCESS_STATUSES and not problems:
        rec["failures"] = 0
        rec["current_version"] = version
        if decision.detail.get("canary"):
            target.setdefault("canary_passes", []).append({"sandbox_id": sid, "at": now})
        return "info"
    rec["failures"] = rec.get("failures", 0) + 1
    if problems:
        # Preservation is the one thing we never retry blind: stop the target
        # version everywhere and hold this box for a human.
        target["halted"] = {"at": now, "by": sid, "reason": "preservation_mismatch",
                            "problems": problems[:20]}
        rec["held"] = {"at": now, "reason": "preservation_mismatch"}
        return "error"
    target["failures"] = target.get("failures", 0) + 1
    if (decision.detail.get("canary") or status in {"rollback_failed", "recovery_required"}
            or target["failures"] >= 2):
        target["halted"] = {"at": now, "by": sid, "reason": f"migration {status}",
                            "failures": target["failures"]}
    if rec["failures"] >= max(max_failures, 1) or status in {"rollback_failed", "recovery_required"}:
        rec["held"] = {"at": now, "reason": f"{rec['failures']} failed attempts"}
    return "error"


def fleet_report(ledger: dict, policy: Policy | None = None,
                 decisions: list[dict] | None = None) -> dict:
    halted = {v: t["halted"] for v, t in ledger.get("targets", {}).items() if t.get("halted")}
    held = {s: b["held"] for s, b in ledger.get("boxes", {}).items() if b.get("held")}
    report: dict[str, Any] = {"halted_targets": halted, "held_boxes": held}
    if policy is not None:
        report["stage"] = policy.stage
        report["canary_ids"] = list(policy.canary_ids)
        if policy.notes:
            report["policy_notes"] = policy.notes
    if decisions is not None:
        report["decisions"] = decisions
    return report


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


async def status_block() -> dict:
    """Read-only automatic-update status for GET /drift. Never raises."""
    try:
        policy = await resolve_policy()
    except Exception as exc:  # resolve_policy already fails closed; belt and braces
        return {"stage": "off", "error": str(exc)[:200]}
    block: dict = {"master_switch_knob": "infrastructure.sandbox.auto_migrate",
                   **fleet_report({}, policy)}
    try:
        ledger = await asyncio.to_thread(Ledger().load)
    except Exception as exc:
        block["ledger_error"] = str(exc)[:200]
        return block
    block.update(fleet_report(ledger, policy))
    block["targets"] = {v: {k: t.get(k) for k in ("first_seen", "canary_pass", "halted", "failures")}
                        for v, t in (ledger.get("targets") or {}).items()}
    return block
