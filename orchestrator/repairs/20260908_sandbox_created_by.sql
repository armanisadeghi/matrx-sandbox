-- Data-only repair, not a schema migration. The canonical access predicate
-- reads created_by, while the orchestrator historically wrote user_id only.
-- September 8 census: 227 missing owners, all with a real auth user and org;
-- six already-populated created_by values matched user_id. Preserve those.
-- PostgresSandboxStore.save now stamps new rows and repairs blank upserts.
UPDATE public.sandbox_instances AS sandbox
SET created_by = sandbox.user_id
WHERE sandbox.created_by IS NULL
  AND sandbox.organization_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM auth.users AS owner WHERE owner.id = sandbox.user_id);
