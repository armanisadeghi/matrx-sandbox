-- Migration 002: tier + template + labels columns on sandbox_instances
--
-- Adds the metadata needed to:
--   1. Distinguish EC2-tier sandboxes from self-hosted (`tier`).
--   2. Record which template variant the sandbox was created from (`template`,
--      `template_version`) so we can migrate users when templates change.
--   3. Carry free-form tags (`labels`) for grouping / observability.
--
-- Apply manually:
--   psql $MATRX_DATABASE_URL -f orchestrator/migrations/002_add_tier_template_columns.sql

ALTER TABLE sandbox_instances
    ADD COLUMN IF NOT EXISTS tier TEXT
        CHECK (tier IN ('ec2', 'hosted'));

ALTER TABLE sandbox_instances
    ADD COLUMN IF NOT EXISTS template TEXT;

ALTER TABLE sandbox_instances
    ADD COLUMN IF NOT EXISTS template_version TEXT;

ALTER TABLE sandbox_instances
    ADD COLUMN IF NOT EXISTS labels JSONB;

CREATE INDEX IF NOT EXISTS idx_sandbox_instances_tier ON sandbox_instances(tier) WHERE tier IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sandbox_instances_template ON sandbox_instances(template) WHERE template IS NOT NULL;
