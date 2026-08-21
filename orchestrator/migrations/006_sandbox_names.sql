-- Migration 006: user-visible sandbox names.
--
-- ``sandbox_id`` remains the immutable routing identity. ``name`` is the
-- owner-editable label shown in user/admin interfaces and compute pickers.

ALTER TABLE public.sandbox_instances
    ADD COLUMN IF NOT EXISTS name TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'sandbox_instances_name_length'
          AND conrelid = 'public.sandbox_instances'::regclass
    ) THEN
        ALTER TABLE public.sandbox_instances
            ADD CONSTRAINT sandbox_instances_name_length
            CHECK (
                name IS NULL
                OR (char_length(btrim(name)) BETWEEN 1 AND 100)
            );
    END IF;
END
$$;

COMMENT ON COLUMN public.sandbox_instances.name IS
    'Owner-editable display name. sandbox_id remains the immutable routing identity.';
