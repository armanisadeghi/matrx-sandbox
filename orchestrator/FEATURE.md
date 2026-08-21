# Sandbox orchestrator

The orchestrator owns one explicit persistence tier: `ec2` (S3 home data) or
`hosted` (Docker-volume home data). `Settings.resolve_host_tier` is the only
resolver for operations that select between them.

**Missing tier fails loudly.** Set `MATRX_HOST_TIER=ec2|hosted`, or pass an
explicit tier only when the caller intentionally targets it. EC2 is not an
equivalent fallback for an unknown hosted-tier identity.

## Change log

- 2026-08-21 — Removed implicit EC2 selection from storage and token routing.
