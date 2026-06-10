"""Orchestrator-mediated sync of central per-user memory ↔ a sandbox.

The user wants a small folder where the agent keeps .md/text files that serve
as persistent memory of the user, their projects, and preferences — the SAME
across every project and every sandbox. That can't live only in an ephemeral
box (slim/EC2 keep no volume), so the canonical copy lives centrally in the
``user_memory`` table (keyed on user_id only) and the orchestrator moves it in
and out of ``/home/agent/.matrx/memory/``:

  - **hydrate** (on create/resume): central rows → files in the box, so a fresh
    box already knows the user.
  - **capture** (on graceful teardown): files in the box → central rows, so
    edits the agent made this session follow the user to the next box.

We use Docker ``put_archive``/``get_archive`` rather than shelling in: it's
binary-safe, needs no quoting, and works the instant the container exists.
Everything here is BEST-EFFORT — memory sync must never block or break the
sandbox lifecycle. Failures are logged and swallowed by the callers.

Guardrails (memory is meant to be SMALL):
  - per-file cap ``MAX_FILE_BYTES`` — bigger files are skipped on capture.
  - total cap ``MAX_TOTAL_BYTES`` — stop capturing past this.
  - text only — non-UTF-8 (binary) files are skipped; ``content`` is a text col.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Where memory lives inside the box.
AGENT_HOME = "/home/agent"
MEMORY_REL = ".matrx/memory"
MEMORY_ABS = f"{AGENT_HOME}/{MEMORY_REL}"

MAX_FILE_BYTES = 256 * 1024        # 256 KB per memory file
MAX_TOTAL_BYTES = 5 * 1024 * 1024  # 5 MB total — memory is notes, not data


async def hydrate_memory_into_container(container, user_id: str, store) -> int:
    """Write the user's central memory into the box's ``.matrx/memory/``.

    Returns the number of files written. Best-effort; never raises.
    """
    try:
        entries = await store.memory_list(user_id)
    except Exception as exc:
        logger.warning("memory hydrate: store.memory_list failed for %s: %s", user_id, exc)
        return 0
    if not entries:
        return 0

    # Build a tar whose members are ".matrx/memory/<path>", extracted at
    # /home/agent so the relative dirs are created for us.
    buf = io.BytesIO()
    written = 0
    try:
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for e in entries:
                rel = _safe_rel(e.get("path", ""))
                if not rel:
                    continue
                data = (e.get("content") or "").encode("utf-8")
                info = tarfile.TarInfo(name=f"{MEMORY_REL}/{rel}")
                info.size = len(data)
                info.mtime = int(datetime.now(timezone.utc).timestamp())
                info.uid = 1000   # agent
                info.gid = 1000
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
                written += 1
        buf.seek(0)
        if written == 0:
            return 0
        await asyncio.to_thread(container.put_archive, AGENT_HOME, buf.getvalue())
        # Make sure agent owns the tree (put_archive sets uid/gid numerically
        # but the .matrx parent dir may pre-exist root-owned from an early boot).
        await asyncio.to_thread(container.exec_run, ["chown", "-R", "agent:agent", f"{AGENT_HOME}/.matrx"])
        logger.info("memory hydrate: wrote %d file(s) into %s for user %s",
                    written, MEMORY_ABS, user_id)
        return written
    except Exception as exc:
        logger.warning("memory hydrate: put_archive failed for %s: %s", user_id, exc)
        return 0


async def capture_memory_from_container(container, user_id: str, store) -> int:
    """Read the box's ``.matrx/memory/`` and upsert it into central memory.

    Returns the number of files captured. Best-effort; never raises. Runs
    BEFORE the container is stopped (the dir must still be readable).
    """
    try:
        bits, _ = await asyncio.to_thread(container.get_archive, MEMORY_ABS)
    except Exception as exc:
        # NotFound just means the box never created/used memory — not an error.
        logger.debug("memory capture: get_archive(%s) for %s: %s", MEMORY_ABS, user_id, exc)
        return 0

    raw = io.BytesIO()
    for chunk in bits:
        raw.write(chunk)
    raw.seek(0)

    captured = 0
    total = 0
    skipped_for_cap = 0
    try:
        # Capture smallest-first so a single big file near the cap doesn't crowd
        # out many small notes — maximize how many memory files survive.
        with tarfile.open(fileobj=raw, mode="r") as tar:
            members = sorted(
                (m for m in tar.getmembers() if m.isfile()),
                key=lambda m: m.size,
            )
            for member in members:
                # get_archive prefixes members with the basename of the path,
                # i.e. "memory/<rel>". Strip that to get the path we store.
                rel = _strip_prefix(member.name, "memory/")
                rel = _safe_rel(rel)
                if not rel:
                    continue
                if member.size > MAX_FILE_BYTES:
                    logger.warning("memory capture: skipping %s (%d bytes > per-file cap)", rel, member.size)
                    skipped_for_cap += 1
                    continue
                if total + member.size > MAX_TOTAL_BYTES:
                    # Don't abandon the rest: a later, smaller file may still
                    # fit under the total cap. Skip just this one and keep going.
                    logger.warning("memory capture: %s would exceed total cap; skipping it", rel)
                    skipped_for_cap += 1
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                data = f.read()
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    logger.debug("memory capture: skipping non-text %s", rel)
                    continue
                try:
                    await store.memory_put(user_id, rel, content)
                    captured += 1
                    total += member.size
                except Exception as exc:
                    logger.warning("memory capture: memory_put failed for %s/%s: %s",
                                   user_id, rel, exc)
        if captured:
            logger.info("memory capture: saved %d file(s) from %s for user %s",
                        captured, MEMORY_ABS, user_id)
        if skipped_for_cap:
            logger.warning(
                "memory capture: %d memory file(s) for user %s were NOT saved "
                "(size caps) — some user memory was dropped",
                skipped_for_cap, user_id,
            )
        return captured
    except Exception as exc:
        logger.warning("memory capture: untar failed for %s: %s", user_id, exc)
        return 0


def _safe_rel(path: str) -> str:
    """Normalize a memory-relative path; reject traversal / absolute paths."""
    if not path:
        return ""
    path = path.strip().lstrip("/")
    parts = []
    for seg in path.split("/"):
        if seg in ("", ".", ".."):
            # Drop empty/./.. — never let memory escape its folder.
            if seg == "..":
                return ""
            continue
        parts.append(seg)
    return "/".join(parts)


def _strip_prefix(name: str, prefix: str) -> str:
    name = name.lstrip("./")
    if name.startswith(prefix):
        return name[len(prefix):]
    return name


__all__ = ["hydrate_memory_into_container", "capture_memory_from_container"]
