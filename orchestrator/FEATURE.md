# Sandbox orchestrator

Cross-repo restoration and acceptance: /Users/armanisadeghi/code/common-docs/systems/infrastructure/sandboxes/REGISTER.md.

`/agent-binding` advertises `MATRX_PUBLIC_URL`, like `/access-tokens`, because
the binding can be used by local and development AI Dream runtimes outside
AWS. Server consumers select their configured per-tier transport after minting;
ECS uses private DNS without imposing that address on external callers.

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

- 2026-09-11 — Access-token and agent-binding issuance now perform one
  per-sandbox Docker liveness check before minting. A durable live row whose
  container vanished is atomically transitioned to `stopped` with an
  actionable resume response; a Docker API outage returns retryable 503 rather
  than issuing credentials for an unverified box. This closes the short window
  before background Postgres boot reconciliation catches stale rows without
  serializing the 215-container census.
- 2026-09-11 — A hosted release with 215 durable sandbox rows spent 157 seconds
  reconciling Docker before Uvicorn could accept traffic. The sole router then
  returned Traefik's ``no available server`` for token mints, dropping sandbox
  tools from chat turns. Postgres is already the request authority, so its boot
  reconciliation now continues in the background; in-memory mode retains the
  synchronous rehydration requirement.
- 2026-09-08 — Portable agent bindings use the public endpoint; private transport remains a server-consumer configuration decision. Regression proves a private address cannot replace a portable binding.

- 2026-08-31 — Sandbox command wrapping now shell-quotes the complete user program before evaluating it, so a valid trailing heredoc remains syntactically intact while the orchestrator still captures the real exit status and post-command working directory.
- 2026-08-27 — Token and agent-binding issuance now pass the persisted literal tier directly to the strict tier resolver. Both endpoints previously dereferenced `.value` on a plain string and returned HTTP 500 for every valid sandbox row.
- 2026-08-27 — Token issuance now projects development connection-hook diagnostics onto a bounded JSON-primitive contract before constructing the response. Hook internals can no longer cause a late response-serialization 500 after a valid token was minted.
- 2026-08-25 — Made both deployment paths refuse a missing or wrong `MATRX_HOST_TIER` before swapping the orchestrator. Token issuance and lifecycle routing require exact tier identity; a bad deployment now stops with the actionable variable name instead of surfacing as an opaque token-mint HTTP 500.
- 2026-08-25 — Made Postgres pool recovery generation-safe and non-blocking: one lock now serializes pool publication, failed pools are detached before graceful close, and retirement has a five-second hard bound with forced termination. This prevents a leaked/closing asyncpg connection from wedging `/health`, removing the only Traefik backend, and turning every sandbox token mint into a misleading plain 404.
- 2026-08-23 — Required explicit organization identity through create, lifecycle, reconcile, and persistence.
- 2026-08-21 — Removed implicit EC2 selection from storage and token routing.
