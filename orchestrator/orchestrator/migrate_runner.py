"""Apply the SQL migrations in orchestrator/migrations/ in order, tracked.

There was no runner before — every migration said "apply manually: psql ...",
so a fresh deploy could run code against a schema missing columns (e.g. the
``deleted_at`` the store queries). This applies any not-yet-applied migration
inside a transaction and records it in a ``schema_migrations`` table, so it is
idempotent and safe to run on every deploy.

Usage::

    python -m orchestrator.migrate_runner            # uses MATRX_DATABASE_URL
    python -m orchestrator.migrate_runner --dry-run  # list pending, apply none

Wire it into the hosted deploy (deploy-hosted.sh) and the EC2 deploy so a push
to main brings the schema forward before the new orchestrator serves traffic.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from orchestrator.config import settings
from orchestrator.logging_config import setup_logging

logger = logging.getLogger("orchestrator.migrate_runner")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _discover() -> list[tuple[str, Path]]:
    """Return ``(version, path)`` for every *.sql, sorted by filename.

    The numeric prefix (001_, 002_, …) defines order; the full filename is the
    recorded version so renames are caught."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
    return [(p.name, p) for p in files]


async def _connect():
    import asyncpg
    from urllib.parse import urlparse, unquote

    if not settings.database_url:
        raise RuntimeError("MATRX_DATABASE_URL is not set")
    parsed = urlparse(settings.database_url)
    return await asyncpg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=parsed.path.lstrip("/") or "postgres",
        statement_cache_size=0,  # Supabase transaction-pooler compatible
    )


async def run_migrations(dry_run: bool = False) -> dict:
    """Apply pending migrations. Returns a summary dict. Idempotent."""
    conn = await _connect()
    summary: dict = {"applied": [], "already": [], "pending": []}
    try:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version     TEXT PRIMARY KEY,
                   applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
               )"""
        )
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}

        for version, path in _discover():
            if version in applied:
                summary["already"].append(version)
                continue
            summary["pending"].append(version)
            if dry_run:
                logger.info("PENDING (dry-run, not applied): %s", version)
                continue
            sql = path.read_text()
            # Each migration + its bookkeeping insert in ONE transaction, so a
            # failure leaves neither the DDL half-applied nor the row recorded.
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", version
                )
            summary["applied"].append(version)
            logger.info("applied migration %s", version)

        if not dry_run:
            logger.info(
                "migrations: applied=%d already=%d",
                len(summary["applied"]), len(summary["already"]),
            )
        return summary
    finally:
        await conn.close()


def main() -> int:
    setup_logging()
    dry_run = "--dry-run" in sys.argv[1:]
    try:
        summary = asyncio.run(run_migrations(dry_run=dry_run))
    except Exception as exc:  # noqa: BLE001 — top-level CLI guard
        logger.error("migration run failed: %s", exc)
        return 1
    if dry_run and summary["pending"]:
        print("Pending migrations:", ", ".join(summary["pending"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
