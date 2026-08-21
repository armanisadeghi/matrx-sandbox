"""Safe SessionStart hooks for the internal permanent development worker."""

from __future__ import annotations

import asyncio
import logging
import time

from orchestrator import activity, sandbox_manager
from orchestrator.models import SandboxResponse

logger = logging.getLogger(__name__)

_locks: dict[str, asyncio.Lock] = {}
_last_results: dict[str, tuple[float, dict]] = {}
_CACHE_SECONDS = 30.0
_SYNC_COMMAND = (
    "flock -w 10 /tmp/matrx-connect-repo-sync.lock "
    "/home/agent/repos/matrx-sandbox/scripts/sync-internal-development-repos.sh"
)


async def prepare_development_connection(sandbox: SandboxResponse) -> dict:
    """Refresh repositories without ever rewriting local work.

    The repository helper fetches every canonical repo and fast-forwards only
    a clean ``main`` that is an ancestor of ``origin/main``. Dirty, detached,
    non-main, and diverged repositories are left byte-for-byte untouched and
    reported as warnings. A short cache coalesces the agent-binding and token
    mint calls that can occur during one UI connection.
    """
    sandbox_id = sandbox.sandbox_id
    now = time.monotonic()
    cached = _last_results.get(sandbox_id)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return {**cached[1], "cached": True}

    lock = _locks.setdefault(sandbox_id, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _last_results.get(sandbox_id)
        if cached and now - cached[0] < _CACHE_SECONDS:
            return {**cached[1], "cached": True}

        result = {
            "hook": "session_start.repo_sync",
            "status": "error",
            "exit_code": None,
            "summary": "",
            "image_refresh": None,
            "cached": False,
        }
        try:
            # Deploys migrate idle development workers immediately. This
            # second event-driven gate closes the only remaining gap: if the
            # worker was busy during deploy, the next fresh connection gets
            # one more safe chance before any new agent tool call begins.
            from orchestrator.migrate import migrate_sandbox

            migration = await migrate_sandbox(
                sandbox_id,
                store=sandbox_manager._get_store(),
                require_idle=True,
            )
            result["image_refresh"] = migration

            async with activity.track(sandbox_id):
                exit_code, stdout, stderr, _cwd = await sandbox_manager.exec_in_sandbox(
                    sandbox_id=sandbox_id,
                    command=_SYNC_COMMAND,
                    timeout=180,
                    user="agent",
                    cwd="/home/agent",
                )
            output = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
            result.update(
                status="ok" if exit_code == 0 else "completed_with_warnings",
                exit_code=exit_code,
                summary=output[-12000:],
            )
        except Exception as exc:
            result["summary"] = str(exc)
            logger.exception("development connection hook failed for %s", sandbox_id)

        _last_results[sandbox_id] = (time.monotonic(), result)
        logger.info(
            "development connection prepared for %s: status=%s exit_code=%s",
            sandbox_id,
            result["status"],
            result["exit_code"],
        )
        return result
