"""Abstract sandbox store and implementations.

Provides a pluggable storage backend for sandbox registry state.
Supports in-memory (default, local dev) and Postgres (production via Supabase).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
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

    async def close(self) -> None:
        """Clean up resources. Override in subclasses that need cleanup."""
        pass


class InMemorySandboxStore(SandboxStore):
    """In-memory sandbox store using a dict. Default for local dev.

    All state is lost on restart — suitable for development only.
    """

    def __init__(self) -> None:
        self._sandboxes: dict[str, SandboxResponse] = {}

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
        """Lazy-initialize the connection pool."""
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
            )
            logger.info("Postgres connection pool created")
        return self._pool

    async def save(self, sandbox: SandboxResponse) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sandbox_instances
                    (user_id, sandbox_id, status, container_id, created_at, hot_path, cold_path, config, ttl_seconds)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
                ON CONFLICT (sandbox_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    container_id = EXCLUDED.container_id,
                    config = EXCLUDED.config,
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
            )

    async def get(self, sandbox_id: str) -> SandboxResponse | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sandbox_instances WHERE sandbox_id = $1",
                sandbox_id,
            )
            if not row:
                return None
            return _row_to_sandbox(row)

    async def list(self, user_id: str | None = None) -> list[SandboxResponse]:
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
        """Find and mark expired sandboxes. Returns list of sandbox_ids that expired."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """UPDATE sandbox_instances
                   SET status = 'expired', stopped_at = NOW(), stop_reason = 'expired'
                   WHERE status IN ('ready', 'running')
                     AND expires_at IS NOT NULL
                     AND expires_at < NOW()
                   RETURNING sandbox_id"""
            )
            expired = [row["sandbox_id"] for row in rows]
            if expired:
                logger.info("Expired %d stale sandboxes: %s", len(expired), expired)
            return expired


def _row_to_sandbox(row) -> SandboxResponse:
    """Convert an asyncpg Row to a SandboxResponse."""
    config_val = row.get("config")
    if isinstance(config_val, str):
        config_val = json.loads(config_val)
    return SandboxResponse(
        sandbox_id=row["sandbox_id"],
        user_id=str(row["user_id"]),
        status=SandboxStatus(row["status"]),
        container_id=row["container_id"],
        created_at=row["created_at"],
        hot_path=row.get("hot_path", "/home/agent"),
        cold_path=row.get("cold_path", "/data/cold"),
        config=config_val or {},
        ttl_seconds=row.get("ttl_seconds", 7200),
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
