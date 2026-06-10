-- Migration 004: user_memory — central, cross-project per-user agent memory
--
-- A tiny key/value store of text files (markdown, notes, preferences) that
-- follows a USER across every project, tier, and sandbox. This is the backing
-- store for /home/agent/.matrx/memory/: the orchestrator hydrates these rows
-- into a sandbox on create and captures edits back on graceful teardown, so an
-- ephemeral box (slim/EC2) carries the user's long-term memory even though it
-- keeps no volume of its own.
--
-- Keyed on user_id + path ONLY — deliberately NOT scoped to project or
-- sandbox, so it's the same memory everywhere. Small by design (text files).
--
-- Lives on the automation-matrix Supabase project (txzxabzwovsujtloxrus),
-- alongside sandbox_instances.
--
-- Apply manually:
--   psql $MATRX_DATABASE_URL -f orchestrator/migrations/004_user_memory.sql

CREATE TABLE IF NOT EXISTS user_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    -- Relative path under .matrx/memory/ (e.g. 'preferences.md',
    -- 'projects/acme.md'). Forward-slash separated; no leading slash.
    path TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    -- Optional free-form tags for grouping/observability.
    labels JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One row per (user, path); upsert target.
    UNIQUE (user_id, path)
);

CREATE INDEX IF NOT EXISTS idx_user_memory_user_id ON user_memory(user_id);

-- RLS — a user can only see/edit their own memory. The orchestrator connects
-- with the service role (bypasses RLS) to hydrate/capture on the user's
-- behalf; the matrx-frontend proxy enforces per-user ownership on top.
ALTER TABLE user_memory ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_memory_select ON user_memory;
CREATE POLICY user_memory_select ON user_memory
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS user_memory_insert ON user_memory;
CREATE POLICY user_memory_insert ON user_memory
    FOR INSERT WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS user_memory_update ON user_memory;
CREATE POLICY user_memory_update ON user_memory
    FOR UPDATE USING (auth.uid() = user_id);

DROP POLICY IF EXISTS user_memory_delete ON user_memory;
CREATE POLICY user_memory_delete ON user_memory
    FOR DELETE USING (auth.uid() = user_id);

-- Auto-update updated_at on edit.
CREATE OR REPLACE FUNCTION update_user_memory_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS user_memory_updated_at ON user_memory;
CREATE TRIGGER user_memory_updated_at
    BEFORE UPDATE ON user_memory
    FOR EACH ROW
    EXECUTE FUNCTION update_user_memory_updated_at();
