-- Migration: Create sandbox_instances table for persistent sandbox registry
-- This table lives in the automation-matrix Supabase project (txzxabzwovsujtloxrus)
-- and tracks ephemeral sandbox containers spun up per user per project.
--
-- Applied via Supabase MCP migration. Manual apply:
--   psql $MATRX_DATABASE_URL -f orchestrator/migrations/001_create_sandboxes.sql

CREATE TABLE IF NOT EXISTS sandbox_instances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
    sandbox_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'creating'
        CHECK (status IN ('creating', 'starting', 'ready', 'running', 'shutting_down', 'stopped', 'failed', 'expired')),
    container_id TEXT,
    hot_path TEXT DEFAULT '/home/agent',
    cold_path TEXT DEFAULT '/data/cold',
    config JSONB DEFAULT '{}',
    ttl_seconds INTEGER NOT NULL DEFAULT 7200,
    expires_at TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ,
    stopped_at TIMESTAMPTZ,
    stop_reason TEXT CHECK (stop_reason IN ('user_requested', 'expired', 'error', 'graceful_shutdown', 'admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sandbox_instances_user_id ON sandbox_instances(user_id);
CREATE INDEX IF NOT EXISTS idx_sandbox_instances_project_id ON sandbox_instances(project_id);
CREATE INDEX IF NOT EXISTS idx_sandbox_instances_status ON sandbox_instances(status);
CREATE INDEX IF NOT EXISTS idx_sandbox_instances_sandbox_id ON sandbox_instances(sandbox_id);
CREATE INDEX IF NOT EXISTS idx_sandbox_instances_expires_at ON sandbox_instances(expires_at) WHERE status IN ('ready', 'running');

-- RLS
ALTER TABLE sandbox_instances ENABLE ROW LEVEL SECURITY;

CREATE POLICY sandbox_instances_select ON sandbox_instances
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY sandbox_instances_insert ON sandbox_instances
    FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY sandbox_instances_update ON sandbox_instances
    FOR UPDATE USING (auth.uid() = user_id);

CREATE POLICY sandbox_instances_delete ON sandbox_instances
    FOR DELETE USING (auth.uid() = user_id);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_sandbox_instances_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sandbox_instances_updated_at
    BEFORE UPDATE ON sandbox_instances
    FOR EACH ROW
    EXECUTE FUNCTION update_sandbox_instances_updated_at();

-- Auto-set expires_at when status transitions to running/ready
CREATE OR REPLACE FUNCTION set_sandbox_expires_at()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status IN ('running', 'ready') AND (OLD.status IS NULL OR OLD.status NOT IN ('running', 'ready')) THEN
        IF NEW.expires_at IS NULL THEN
            NEW.expires_at = NOW() + (NEW.ttl_seconds || ' seconds')::INTERVAL;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sandbox_instances_set_expires
    BEFORE UPDATE ON sandbox_instances
    FOR EACH ROW
    EXECUTE FUNCTION set_sandbox_expires_at();
