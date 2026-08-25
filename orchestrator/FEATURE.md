# Sandbox orchestrator

The orchestrator owns one explicit persistence tier: `ec2` (S3 home data) or
`hosted` (Docker-volume home data). `Settings.resolve_host_tier` is the only
resolver for operations that select between them.

**Missing tier fails loudly.** Set `MATRX_HOST_TIER=ec2|hosted`, or pass an
explicit tier only when the caller intentionally targets it. EC2 is not an
equivalent fallback for an unknown hosted-tier identity.

**Every sandbox has explicit organization identity.** `CreateSandboxRequest`,
`SandboxResponse`, container labels, and `SandboxStore.save` require the
initiating `organization_id`; reset, resume, and reconcile preserve that field.
Postgres never receives an org-less sandbox write. Read the emergency contract:
[`no-db-assigned-org/PLAN.md`](../../common-docs/projects/no-db-assigned-org/PLAN.md).

## Change log

- 2026-08-25 — Made both deployment paths refuse a missing or wrong `MATRX_HOST_TIER` before swapping the orchestrator. Token issuance and lifecycle routing require exact tier identity; a bad deployment now stops with the actionable variable name instead of surfacing as an opaque token-mint HTTP 500.
- 2026-08-25 — Made Postgres pool recovery generation-safe and non-blocking: one lock now serializes pool publication, failed pools are detached before graceful close, and retirement has a five-second hard bound with forced termination. This prevents a leaked/closing asyncpg connection from wedging `/health`, removing the only Traefik backend, and turning every sandbox token mint into a misleading plain 404.
- 2026-08-23 — Required explicit organization identity through create, lifecycle, reconcile, and persistence.
- 2026-08-21 — Removed implicit EC2 selection from storage and token routing.
