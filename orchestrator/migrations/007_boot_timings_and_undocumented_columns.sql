-- Migration 007: how long a box actually takes, and the columns nobody wrote down.
--
-- PART A — THE UNDOCUMENTED COLUMNS. ``store.py`` has written
-- ``organization_id`` and ``created_by`` on every INSERT since the
-- explicit-organization work, and NO file in this directory ever created them:
-- 001 does not, and neither does 002–006. They exist in the live database
-- because they were added out of band, which means a fresh Postgres built from
-- these migrations alone would have rejected every sandbox create. This is the
-- honest creating migration for them — idempotent, so the live database where
-- they already exist is unchanged, and a fresh one finally matches the code.
-- (``updated_by``, ``version``, ``metadata``, ``custom_fields``, ``project_id``
-- and ``task_id`` are also live on this table and also absent from these files;
-- they arrive from the platform's canonical entity-table shape rather than from
-- this orchestrator, which never writes them, so they are named here in a
-- comment rather than re-created by a service that does not own them.)
--
-- PART B — THE MEASUREMENT. Nothing in this repository has ever measured how
-- long a sandbox takes to become usable. The only real figure anywhere was
-- aidream's remark that "a cold aidream box takes minutes", and the sandbox
-- VISION doc still advertised "~0.5 s from the warm pool" — a warm pool that
-- was retired on 2026-09-17. "Creation and teardown need to be easy and fast…
-- The data and all of that should already be very fast but check" (Arman,
-- 2026-09-20) cannot be answered, or honestly disputed, without numbers.
--
-- So the row now records, nullable and never blocking anything:
--   ready_at            — when the box first reported itself usable
--   boot_seconds        — create/resume → ready, wall clock, one number
--   boot_kind           — which journey that number describes ('create' | 'resume')
--   boot_phase_seconds  — {phase: seconds} for the phases the box reported
--
-- Every one is NULL for a box that never became ready, and NULL for every box
-- that already exists. A NULL here means "not measured", never "instant".

ALTER TABLE public.sandbox_instances
    ADD COLUMN IF NOT EXISTS organization_id UUID,
    ADD COLUMN IF NOT EXISTS created_by UUID,
    ADD COLUMN IF NOT EXISTS ready_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS boot_seconds DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS boot_kind TEXT,
    ADD COLUMN IF NOT EXISTS boot_phase_seconds JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'sandbox_instances_boot_kind'
          AND conrelid = 'public.sandbox_instances'::regclass
    ) THEN
        ALTER TABLE public.sandbox_instances
            ADD CONSTRAINT sandbox_instances_boot_kind
            CHECK (boot_kind IS NULL OR boot_kind IN ('create', 'resume'));
    END IF;
END
$$;

COMMENT ON COLUMN public.sandbox_instances.organization_id IS
    'The tenant this sandbox belongs to. Supplied explicitly by the initiating '
    'request; the database never assigns it.';
COMMENT ON COLUMN public.sandbox_instances.created_by IS
    'Canonical access reads this, not user_id. Written from the persisted owner.';
COMMENT ON COLUMN public.sandbox_instances.ready_at IS
    'When this box first reported itself usable. NULL means not measured, never instant.';
COMMENT ON COLUMN public.sandbox_instances.boot_seconds IS
    'Wall clock from the start of the create/resume to ready_at, in seconds.';
COMMENT ON COLUMN public.sandbox_instances.boot_kind IS
    'Which journey boot_seconds measures: create (cold) or resume (same volume).';
COMMENT ON COLUMN public.sandbox_instances.boot_phase_seconds IS
    'Seconds spent in each boot phase the box reported '
    '(container, home_sync, cold_mount, environment, sdk, cloud_files). '
    'Partial by nature: a box on an image without phase markers reports none.';
