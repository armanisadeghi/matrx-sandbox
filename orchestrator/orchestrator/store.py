"""Abstract sandbox store and implementations.

Provides a pluggable storage backend for sandbox registry state.
Supports in-memory (default, local dev) and Postgres (production via Supabase).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from uuid import UUID

from orchestrator.models import SandboxResponse, SandboxStatus

logger = logging.getLogger(__name__)


class SandboxStore(ABC):
    """Abstract base class for sandbox persistence."""

    @abstractmethod
    async def save(self, sandbox: SandboxResponse) -> None:
        """Save or update a sandbox record."""

    @abstractmethod
    async def get(self, sandbox_id: str) -> SandboxResponse | None:
        """Get a sandbox by ID. Returns None if not found."""

    @abstractmethod
    async def list(self, user_id: str | None = None) -> list[SandboxResponse]:
        """List all sandboxes, optionally filtered by user_id."""

    @abstractmethod
    async def delete(self, sandbox_id: str) -> bool:
        """Delete a sandbox record. Returns True if deleted."""

    @abstractmethod
    async def update_status(self, sandbox_id: str, status: SandboxStatus) -> bool:
        """Update just the status of a sandbox. Returns True if found and updated."""

    async def update_heartbeat(self, sandbox_id: str) -> bool:
        """Record a heartbeat timestamp. Returns True if found and updated."""
        return await self.update_status(sandbox_id, SandboxStatus.RUNNING)

    async def mark_stopped(self, sandbox_id: str, reason: str) -> bool:
        """Mark a sandbox as stopped with a reason. Returns True if found and updated."""
        return await self.update_status(sandbox_id, SandboxStatus.STOPPED)

    @abstractmethod
    async def extend_ttl(self, sandbox_id: str, ttl_seconds: int) -> datetime | None:
        """Set ``expires_at = now() + ttl_seconds`` and persist ``ttl_seconds``.

        Returns the new ``expires_at`` value, or ``None`` if the sandbox was not found.
        """

    async def get_lifecycle(self, sandbox_id: str) -> dict | None:
        """Return ``{"status": str, "deleted": bool}`` for a sandbox, or None
        if no row exists. Lets the reconciler tell "user deleted this" apart
        from "this is just untracked" without expanding SandboxResponse with
        a ``deleted_at`` field. Default implementation derives it from
        ``get()``; the Postgres store overrides to read ``deleted_at``.
        """
        sb = await self.get(sandbox_id)
        if sb is None:
            return None
        return {"status": getattr(sb.status, "value", sb.status), "deleted": False}

    async def expire_stale(self) -> list[str]:
        """Mark every sandbox past ``expires_at`` (still in a live status) as
        EXPIRED and return their ids.

        This is the store half of the expiry lifecycle: the reaper calls it
        on an interval, then physically tears down each returned container.
        EXPIRED is a *resumable* terminal state — the per-user volume is kept
        — distinct from a soft-deleted row (gone, volume wipe-eligible).

        Default implementation scans ``list()``; the Postgres store overrides
        with a single atomic UPDATE so concurrent reapers can't double-claim.
        """
        now = datetime.now(timezone.utc)
        expired: list[str] = []
        for sb in await self.list():
            status = getattr(sb.status, "value", sb.status)
            if status in ("ready", "running") and sb.expires_at and sb.expires_at < now:
                await self.update_status(sb.sandbox_id, SandboxStatus.EXPIRED)
                expired.append(sb.sandbox_id)
        return expired

    # ── User memory (central, cross-project) ─────────────────────────────────
    # A tiny per-user text-file store that backs /home/agent/.matrx/memory/.
    # Keyed on user_id + path ONLY, so it's the same memory across every
    # project/tier/sandbox. The orchestrator hydrates it into a box on create
    # and captures edits back on graceful teardown. See orchestrator/memory_sync.py.

    async def memory_list(self, user_id: str) -> list[dict]:
        """Return all memory entries for a user as
        ``[{"path", "content", "updated_at"}]``. Override in subclasses."""
        raise NotImplementedError

    async def memory_put(self, user_id: str, path: str, content: str) -> None:
        """Upsert one memory entry. Override in subclasses."""
        raise NotImplementedError

    async def memory_delete(self, user_id: str, path: str) -> bool:
        """Delete one memory entry; return True if it existed. Override in subclasses."""
        raise NotImplementedError

    async def close(self) -> None:
        """Clean up resources. Override in subclasses that need cleanup."""
        pass


class InMemorySandboxStore(SandboxStore):
    """In-memory sandbox store using a dict. Default for local dev.

    All state is lost on restart — suitable for development only.
    """

    def __init__(self) -> None:
        self._sandboxes: dict[str, SandboxResponse] = {}
        # user_id -> {path -> (content, updated_at)}
        self._memory: dict[str, dict[str, tuple[str, datetime]]] = {}

    async def save(self, sandbox: SandboxResponse) -> None:
        self._sandboxes[sandbox.sandbox_id] = sandbox

    async def get(self, sandbox_id: str) -> SandboxResponse | None:
        return self._sandboxes.get(sandbox_id)

    async def list(self, user_id: str | None = None) -> list[SandboxResponse]:
        sandboxes = list(self._sandboxes.values())
        if user_id:
            sandboxes = [s for s in sandboxes if s.user_id == user_id]
        return sandboxes

    async def delete(self, sandbox_id: str) -> bool:
        return self._sandboxes.pop(sandbox_id, None) is not None

    async def update_status(self, sandbox_id: str, status: SandboxStatus) -> bool:
        sandbox = self._sandboxes.get(sandbox_id)
        if not sandbox:
            return False
        sandbox.status = status
        self._sandboxes[sandbox_id] = sandbox
        return True

    async def mark_stopped(self, sandbox_id: str, reason: str) -> bool:
        sandbox = self._sandboxes.get(sandbox_id)
        if not sandbox:
            return False
        sandbox.status = SandboxStatus.STOPPED
        self._sandboxes[sandbox_id] = sandbox
        return True

    async def extend_ttl(self, sandbox_id: str, ttl_seconds: int) -> datetime | None:
        sandbox = self._sandboxes.get(sandbox_id)
        if not sandbox:
            return None
        new_expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        sandbox.ttl_seconds = ttl_seconds
        sandbox.expires_at = new_expires
        self._sandboxes[sandbox_id] = sandbox
        return new_expires

    async def memory_list(self, user_id: str) -> list[dict]:
        entries = self._memory.get(user_id, {})
        return [
            {"path": path, "content": content, "updated_at": updated_at}
            for path, (content, updated_at) in sorted(entries.items())
        ]

    async def memory_put(self, user_id: str, path: str, content: str) -> None:
        self._memory.setdefault(user_id, {})[path] = (content, datetime.now(timezone.utc))

    async def memory_delete(self, user_id: str, path: str) -> bool:
        return self._memory.get(user_id, {}).pop(path, None) is not None


class PostgresSandboxStore(SandboxStore):
    """Postgres-backed sandbox store using asyncpg.

    Connects to the automation-matrix Supabase project for persistent
    sandbox state that survives orchestrator restarts.

    Table: sandbox_instances (with RLS, triggers for updated_at and expires_at).
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool = None

    async def _get_pool(self):
        """Lazy-initialize the connection pool.

        Supabase's transaction pooler closes idle connections aggressively
        (~30s). Without ``max_inactive_connection_lifetime`` and a
        ``setup`` health check, the pool hands out stale connections that
        fail mid-query with ``ConnectionDoesNotExistError`` — which is
        exactly what wedged the orchestrator for 3 weeks of consecutive
        healthcheck failures before this fix.
        """
        if self._pool is None:
            import asyncpg
            from urllib.parse import urlparse, unquote

            parsed = urlparse(self._database_url)
            self._pool = await asyncpg.create_pool(
                host=parsed.hostname,
                port=parsed.port or 5432,
                user=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                database=parsed.path.lstrip("/") or "postgres",
                min_size=2,
                max_size=10,
                # Disable prepared statements for Supabase transaction pooler compatibility
                statement_cache_size=0,
                # Recycle connections aggressively — Supabase pooler closes
                # them faster than we'd ever want to reuse one. 20s is
                # comfortably under the typical pooler idle-kill window.
                max_inactive_connection_lifetime=20.0,
                # Quick noop on every acquire so the caller never gets a
                # dead-on-arrival connection. asyncpg automatically
                # discards + replaces a connection whose setup raises.
                setup=lambda conn: conn.execute("SELECT 1"),
            )
            logger.info("Postgres connection pool created (idle_lifetime=20s, setup-probed)")
        return self._pool

    async def _execute_with_retry(self, fn, *args, **kwargs):
        """Run a connection-using function with one automatic retry on the
        Supabase pooler's "connection died mid-operation" exception family.

        asyncpg occasionally hands out a connection that the pooler has
        already closed; the first query raises ``ConnectionDoesNotExistError``
        or ``InterfaceError``. We close the pool and rebuild it once, then
        re-run the operation. If the second attempt also fails, we let the
        exception propagate — the route layer surfaces it to the caller.
        """
        import asyncpg
        try:
            return await fn(*args, **kwargs)
        except (
            asyncpg.exceptions.ConnectionDoesNotExistError,
            asyncpg.exceptions.InterfaceError,
            ConnectionResetError,
            BrokenPipeError,
        ) as exc:
            logger.warning("Postgres connection lost (%s); rebuilding pool and retrying once", exc)
            try:
                if self._pool is not None:
                    await self._pool.close()
            except Exception:
                pass
            self._pool = None
            return await fn(*args, **kwargs)

    async def save(self, sandbox: SandboxResponse) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            # ``persistence_volume`` is added in migration 003 and is nullable
            # in old environments — wrapped in COALESCE on update so the
            # UPDATE branch tolerates missing values.
            await conn.execute(
                """
                INSERT INTO sandbox_instances
                    (user_id, sandbox_id, status, container_id, created_at, hot_path, cold_path,
                     config, ttl_seconds, tier, template, template_version, labels,
                     persistence_volume)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13::jsonb, $14)
                ON CONFLICT (sandbox_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    container_id = EXCLUDED.container_id,
                    config = EXCLUDED.config,
                    tier = COALESCE(EXCLUDED.tier, sandbox_instances.tier),
                    template = COALESCE(EXCLUDED.template, sandbox_instances.template),
                    template_version = COALESCE(EXCLUDED.template_version, sandbox_instances.template_version),
                    labels = COALESCE(EXCLUDED.labels, sandbox_instances.labels),
                    persistence_volume = COALESCE(EXCLUDED.persistence_volume, sandbox_instances.persistence_volume),
                    updated_at = NOW()
                """,
                UUID(sandbox.user_id),
                sandbox.sandbox_id,
                sandbox.status.value,
                sandbox.container_id,
                sandbox.created_at,
                sandbox.hot_path,
                sandbox.cold_path,
                json.dumps(sandbox.config) if sandbox.config else '{}',
                sandbox.ttl_seconds,
                sandbox.tier,
                sandbox.template,
                sandbox.template_version,
                json.dumps(sandbox.labels) if sandbox.labels else None,
                sandbox.persistence_volume,
            )

    async def get(self, sandbox_id: str) -> SandboxResponse | None:
        async def _do() -> SandboxResponse | None:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM sandbox_instances WHERE sandbox_id = $1",
                    sandbox_id,
                )
                if not row:
                    return None
                return _row_to_sandbox(row)
        return await self._execute_with_retry(_do)

    async def list(self, user_id: str | None = None) -> list[SandboxResponse]:
        async def _do() -> list[SandboxResponse]:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                if user_id:
                    rows = await conn.fetch(
                        "SELECT * FROM sandbox_instances WHERE user_id = $1 ORDER BY created_at DESC",
                        UUID(user_id),
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT * FROM sandbox_instances ORDER BY created_at DESC"
                    )
                return [_row_to_sandbox(row) for row in rows]
        # ``list()`` is the hot path that wedged the orchestrator for 3 weeks
        # in production. Wrap it explicitly so a dropped pool connection
        # rebuilds the pool transparently. Other methods rely on the pool's
        # max_inactive_connection_lifetime + setup-probe.
        return await self._execute_with_retry(_do)

    async def delete(self, sandbox_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sandbox_instances WHERE sandbox_id = $1",
                sandbox_id,
            )
            return result == "DELETE 1"

    async def update_status(self, sandbox_id: str, status: SandboxStatus) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if status == SandboxStatus.STOPPED:
                result = await conn.execute(
                    """UPDATE sandbox_instances
                       SET status = $1, stopped_at = NOW()
                       WHERE sandbox_id = $2""",
                    status.value,
                    sandbox_id,
                )
            else:
                result = await conn.execute(
                    "UPDATE sandbox_instances SET status = $1 WHERE sandbox_id = $2",
                    status.value,
                    sandbox_id,
                )
            return result == "UPDATE 1"

    async def update_heartbeat(self, sandbox_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE sandbox_instances SET last_heartbeat_at = NOW() WHERE sandbox_id = $1",
                sandbox_id,
            )
            return result == "UPDATE 1"

    async def mark_stopped(self, sandbox_id: str, reason: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE sandbox_instances
                   SET status = 'stopped', stopped_at = NOW(), stop_reason = $1
                   WHERE sandbox_id = $2""",
                reason,
                sandbox_id,
            )
            return result == "UPDATE 1"

    async def extend_ttl(self, sandbox_id: str, ttl_seconds: int) -> datetime | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE sandbox_instances
                   SET ttl_seconds = $1,
                       expires_at = NOW() + ($1 || ' seconds')::INTERVAL
                   WHERE sandbox_id = $2
                   RETURNING expires_at""",
                ttl_seconds,
                sandbox_id,
            )
            if not row:
                return None
            return row["expires_at"]

    async def get_lifecycle(self, sandbox_id: str) -> dict | None:
        async def _do() -> dict | None:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status, deleted_at FROM sandbox_instances WHERE sandbox_id = $1",
                    sandbox_id,
                )
                if not row:
                    return None
                return {"status": row["status"], "deleted": row["deleted_at"] is not None}
        return await self._execute_with_retry(_do)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Postgres connection pool closed")

    async def reconcile(self, running_container_ids: set[str]) -> None:
        """Reconcile DB state with actual Docker containers.

        - Sandboxes marked READY/RUNNING but no matching container -> mark STOPPED
        - Containers running but not in DB -> log warning (don't auto-destroy)
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            active_rows = await conn.fetch(
                "SELECT sandbox_id, container_id FROM sandbox_instances WHERE status IN ('ready', 'running', 'starting')"
            )

            for row in active_rows:
                sandbox_id = row["sandbox_id"]
                container_id = row["container_id"]

                if container_id and container_id not in running_container_ids:
                    await conn.execute(
                        """UPDATE sandbox_instances
                           SET status = 'stopped', stopped_at = NOW(), stop_reason = 'graceful_shutdown'
                           WHERE sandbox_id = $1""",
                        sandbox_id,
                    )
                    logger.warning(
                        "Reconciled sandbox %s: marked STOPPED (container gone)",
                        sandbox_id,
                    )

        logger.info("Store reconciliation complete")

    async def expire_stale(self) -> list[str]:
        """Find and mark expired sandboxes. Returns list of sandbox_ids that expired.

        Wrapped in ``_execute_with_retry`` because the reaper calls this from
        an otherwise-idle loop every 60s — exactly the access pattern that
        trips Supabase's idle-connection reaping. The retry rebuilds the pool
        and runs once more rather than letting the whole sweep fail.
        """
        async def _do() -> list[str]:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """UPDATE sandbox_instances
                       SET status = 'expired', stopped_at = NOW(), stop_reason = 'expired'
                       WHERE status IN ('ready', 'running')
                         AND expires_at IS NOT NULL
                         AND expires_at < NOW()
                         AND deleted_at IS NULL
                       RETURNING sandbox_id"""
                )
                expired = [row["sandbox_id"] for row in rows]
                if expired:
                    logger.info("Expired %d stale sandboxes: %s", len(expired), expired)
                return expired
        return await self._execute_with_retry(_do)

    async def memory_list(self, user_id: str) -> list[dict]:
        async def _do() -> list[dict]:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT path, content, updated_at FROM user_memory
                       WHERE user_id = $1 ORDER BY path""",
                    UUID(user_id),
                )
                return [
                    {"path": r["path"], "content": r["content"], "updated_at": r["updated_at"]}
                    for r in rows
                ]
        return await self._execute_with_retry(_do)

    async def memory_put(self, user_id: str, path: str, content: str) -> None:
        async def _do() -> None:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO user_memory (user_id, path, content)
                       VALUES ($1, $2, $3)
                       ON CONFLICT (user_id, path) DO UPDATE
                       SET content = EXCLUDED.content, updated_at = NOW()""",
                    UUID(user_id), path, content,
                )
        await self._execute_with_retry(_do)

    async def memory_delete(self, user_id: str, path: str) -> bool:
        async def _do() -> bool:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM user_memory WHERE user_id = $1 AND path = $2",
                    UUID(user_id), path,
                )
                return result == "DELETE 1"
        return await self._execute_with_retry(_do)


def _row_to_sandbox(row) -> SandboxResponse:
    """Convert an asyncpg Row to a SandboxResponse."""
    def _maybe(key: str, default=None):
        # asyncpg Row supports __contains__ but not .get on older versions
        try:
            return row[key]
        except (KeyError, IndexError):
            return default

    config_val = _maybe("config")
    if isinstance(config_val, str):
        config_val = json.loads(config_val)

    labels_val = _maybe("labels")
    if isinstance(labels_val, str):
        labels_val = json.loads(labels_val)

    return SandboxResponse(
        sandbox_id=row["sandbox_id"],
        user_id=str(row["user_id"]),
        status=SandboxStatus(row["status"]),
        container_id=row["container_id"],
        created_at=row["created_at"],
        hot_path=_maybe("hot_path") or "/home/agent",
        cold_path=_maybe("cold_path") or "/data/cold",
        config=config_val or {},
        ttl_seconds=_maybe("ttl_seconds") or 7200,
        expires_at=_maybe("expires_at"),
        tier=_maybe("tier"),
        template=_maybe("template"),
        template_version=_maybe("template_version"),
        labels=labels_val,
        persistence_volume=_maybe("persistence_volume"),
    )


def create_store() -> SandboxStore:
    """Factory function to create the appropriate store based on config."""
    from orchestrator.config import settings

    if settings.sandbox_store == "postgres":
        if not settings.database_url:
            raise RuntimeError(
                "MATRX_DATABASE_URL must be set when MATRX_SANDBOX_STORE=postgres"
            )
        logger.info("Using Postgres sandbox store")
        return PostgresSandboxStore(settings.database_url)
    else:
        logger.info("Using in-memory sandbox store (state lost on restart)")
        return InMemorySandboxStore()
