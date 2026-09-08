-- UNAPPLIED REVIEW/TEST FIXTURE. Not a production migration record.
-- Requires review against the store CAS transaction protocol before any DDL.
CREATE OR REPLACE FUNCTION iam._guard_sandbox_migration()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public, iam
AS $guard$
DECLARE
  old_row jsonb := CASE WHEN TG_OP = 'INSERT' THEN '{}'::jsonb ELSE to_jsonb(OLD) END;
  new_row jsonb := CASE WHEN TG_OP = 'DELETE' THEN '{}'::jsonb ELSE to_jsonb(NEW) END;
  old_config jsonb := COALESCE(old_row->'config', '{}'::jsonb);
  new_config jsonb := COALESCE(new_row->'config', '{}'::jsonb);
  old_journal jsonb := old_config->'_migration';
  new_journal jsonb := new_config->'_migration';
  old_phase text := old_journal->>'phase';
  new_phase text := new_journal->>'phase';
  trusted boolean := current_user IN ('postgres', 'service_role');
  active boolean := old_config ? '_migration'
    AND COALESCE(old_phase NOT IN ('complete', 'rolled_back'), true);
  journal_changed boolean := (old_config ? '_migration') IS DISTINCT FROM (new_config ? '_migration')
    OR old_journal IS DISTINCT FROM new_journal;
  marker_op text := nullif(current_setting('matrx.migration_op', true), '');
  marker_phase text := current_setting('matrx.migration_expected_phase', true);
  expected_op text;
  allowed text[];
  heartbeat_columns text[] := ARRAY['updated_at', 'updated_by', 'version', 'last_heartbeat_at'];
BEGIN
  -- Role is PostgreSQL's actual invoking role, never a JWT claim or settable GUC.
  -- INSERT cannot introduce a journal; only a CAS UPDATE of an existing row can.
  IF TG_OP = 'INSERT' THEN
    IF new_config ? '_migration' THEN
      RAISE EXCEPTION 'Migration journals require an existing-row CAS claim' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    IF active THEN
      RAISE EXCEPTION 'Sandbox has an unresolved migration; deletion refused' USING ERRCODE = '42501';
    END IF;
    RETURN OLD;
  END IF;

  IF journal_changed THEN
    IF NOT trusted THEN
      RAISE EXCEPTION 'Migration journal is server-owned' USING ERRCODE = '42501';
    END IF;
    IF NOT (new_config ? '_migration') OR jsonb_typeof(new_journal) <> 'object'
       OR COALESCE(new_journal->>'op_id', '') = '' THEN
      RAISE EXCEPTION 'Migration journal cannot be erased or malformed' USING ERRCODE = '42501';
    END IF;
    expected_op := CASE WHEN old_phase IS NULL OR old_phase IN ('complete', 'rolled_back')
                        THEN new_journal->>'op_id' ELSE old_journal->>'op_id' END;
    IF marker_op IS DISTINCT FROM expected_op
       OR marker_phase IS DISTINCT FROM COALESCE(old_phase, '') THEN
      RAISE EXCEPTION 'Migration change lacks exact transaction-local CAS context' USING ERRCODE = '42501';
    END IF;
    IF old_phase IS NOT NULL AND old_phase NOT IN ('complete', 'rolled_back')
       AND new_journal->>'op_id' IS DISTINCT FROM old_journal->>'op_id' THEN
      RAISE EXCEPTION 'Active migration operation cannot be replaced' USING ERRCODE = '42501';
    END IF;
    allowed := CASE COALESCE(old_phase, '')
      WHEN '' THEN ARRAY['claimed']
      WHEN 'complete' THEN ARRAY['claimed']
      WHEN 'rolled_back' THEN ARRAY['claimed']
      WHEN 'claimed' THEN ARRAY['pause_pending', 'rollback_pending']
      WHEN 'pause_pending' THEN ARRAY['source_paused', 'rollback_pending']
      WHEN 'source_paused' THEN ARRAY['archive_pending', 'rollback_pending']
      WHEN 'archive_pending' THEN ARRAY['archive_verified', 'rollback_pending']
      WHEN 'archive_verified' THEN ARRAY['candidate_create_pending', 'rollback_pending']
      WHEN 'candidate_create_pending' THEN ARRAY['candidate_created', 'rollback_pending']
      WHEN 'candidate_created' THEN ARRAY['candidate_restored', 'rollback_pending']
      WHEN 'candidate_restored' THEN ARRAY['candidate_start_pending', 'rollback_pending']
      WHEN 'candidate_start_pending' THEN ARRAY['candidate_verified', 'rollback_pending']
      WHEN 'candidate_verified' THEN ARRAY['rename_pending', 'rollback_pending']
      WHEN 'rename_pending' THEN ARRAY['candidate_named', 'rollback_pending']
      WHEN 'candidate_named' THEN ARRAY['committed', 'rollback_pending']
      WHEN 'committed' THEN ARRAY['cleanup_pending']
      WHEN 'cleanup_pending' THEN ARRAY['complete']
      WHEN 'rollback_pending' THEN ARRAY['rolled_back']
      ELSE ARRAY[]::text[] END;
    IF new_phase IS NULL OR NOT (new_phase = ANY(allowed)) THEN
      RAISE EXCEPTION 'Invalid migration checkpoint transition' USING ERRCODE = '42501';
    END IF;
  END IF;

  -- Full-row freeze, not only config: identity, ownership, container, lifecycle,
  -- storage, and future columns cannot change incidentally while migration owns it.
  -- Expiry may follow a real heartbeat; ttl_seconds itself remains frozen.
  IF new_row->'last_heartbeat_at' IS DISTINCT FROM old_row->'last_heartbeat_at' THEN
    heartbeat_columns := array_append(heartbeat_columns, 'expires_at');
  END IF;
  IF active AND (new_row - heartbeat_columns) IS DISTINCT FROM (old_row - heartbeat_columns) THEN
    IF NOT trusted OR NOT journal_changed
       OR marker_op IS DISTINCT FROM old_journal->>'op_id'
       OR marker_phase IS DISTINCT FROM old_phase THEN
      RAISE EXCEPTION 'Sandbox lifecycle is fenced by migration' USING ERRCODE = '42501';
    END IF;
  END IF;
  RETURN NEW;
END
$guard$;

CREATE TRIGGER _guard_sandbox_migration
BEFORE INSERT OR UPDATE OR DELETE ON public.sandbox_instances
FOR EACH ROW EXECUTE FUNCTION iam._guard_sandbox_migration();
