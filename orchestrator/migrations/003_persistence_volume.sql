-- Migration 003: persistence_volume column on sandbox_instances
--
-- Records the per-user Docker volume name backing /home/agent for hosted-tier
-- sandboxes. EC2-tier sandboxes leave this NULL and use the S3 prefix model
-- (config.tier='ec2' + S3_BUCKET env var) instead.
--
-- This is part of Phase 1 of docs/PERSISTENCE_PLAN.md — closing the urgent
-- gap where hosted-tier sandboxes had zero persistence and lost all data on
-- container destroy.
--
-- Apply manually:
--   psql $MATRX_DATABASE_URL -f orchestrator/migrations/003_persistence_volume.sql

ALTER TABLE sandbox_instances
    ADD COLUMN IF NOT EXISTS persistence_volume TEXT;

CREATE INDEX IF NOT EXISTS idx_sandbox_instances_persistence_volume
    ON sandbox_instances(persistence_volume)
    WHERE persistence_volume IS NOT NULL;
