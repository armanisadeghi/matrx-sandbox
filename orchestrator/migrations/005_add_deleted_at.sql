-- Migration 005: deleted_at column on sandbox_instances
--
-- The Postgres store already READS and FILTERS on this column:
--   * get_lifecycle()  → "SELECT status, deleted_at ..." (store.py)
--   * list() / reconcile queries → "... AND deleted_at IS NULL"
-- but no migration ever created it. On a database built only from migrations
-- 001-004 those queries raise "column deleted_at does not exist", so the
-- soft-delete / lifecycle path is broken (or the live DB was patched by hand
-- out of band, leaving the repo not matching reality). This migration makes the
-- repo the source of truth either way; it is idempotent.
--
-- NOTE: as of this migration `delete()` still performs a HARD `DELETE FROM`, so
-- nothing SETS deleted_at yet — `deleted` is therefore always False in practice.
-- Wiring delete() to a soft-delete (SET deleted_at = NOW()) is a separate,
-- intentional behavior change tracked in the audit (resume-of-deleted 409 path).
--
-- Apply manually:
--   psql $MATRX_DATABASE_URL -f orchestrator/migrations/005_add_deleted_at.sql

ALTER TABLE sandbox_instances
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Most queries filter `deleted_at IS NULL`; a partial index keeps that cheap
-- without indexing the (rare) soft-deleted rows.
CREATE INDEX IF NOT EXISTS idx_sandbox_instances_deleted_at
    ON sandbox_instances(deleted_at)
    WHERE deleted_at IS NOT NULL;
