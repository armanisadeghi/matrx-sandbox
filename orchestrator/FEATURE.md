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
  serializing the 215-container census. Docker `created` and `restarting`
  states are transitional (not vanished): they retain the row and return 503
  until the container is running.
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

## The sandbox↔browser join (2026-09-20)

A box drives the person's OWN persistent cloud browser, not a throwaway
Chromium. `browser_profile.py` asks AI Dream
`GET /api/sandboxes/internal/default-browser-profile` over the sandbox bridge
(service token + `X-Matrx-User-Id` + a membership-PROVED `X-Organization-Id`)
for the default `browser.profile` in this box's organization, and the create
path injects `MATRX_BROWSER_PROFILE_ID` + `MATRX_BROWSER_EXECUTION_TARGET`.
Never a DB read from this host: a second reader of `browser.profile` would be a
second opinion on who owns which browser.

Both names are on `ORCHESTRATOR_MANAGED_ENV`, and `vault_env_refresh`
re-resolves the pair at EVERY binding and UNSETS both when the browser is gone —
a person can create, rename or delete their browser long after the box was born.
No browser yet means NEITHER name is injected (never a guessed id, never half a
pair); the reason is stamped on `config.browser_profile` and the in-box client
refuses with a sentence telling the person how to get one. Guards:
`tests/test_browser_profile_join.py`.

## Boot measurement (2026-09-20)

Nothing here measured time-to-ready before this date. `_wait_for_ready` now
records, on the row, `ready_at`, `boot_seconds` (from the CALLER's clock — the
moment the create/resume began), `boot_kind` (`create` | `resume`, never
averaged together) and `boot_phase_seconds`. All nullable: **NULL means NOT
MEASURED, never "instant"**. Migration `007`; the same migration honestly
creates `organization_id` and `created_by`, which `store.py` had been writing
with no migration file behind them. The upsert COALESCEs the four columns so a
later save (a heartbeat, a status change) cannot erase the number. Guards:
`tests/test_boot_measurement.py`.

First real figures, live orchestrators, admin test user, template `slim`:
EC2 create→ready 3.7 s, resume→ready 3.3 s / 4.9 s; hosted create→ready
4.8–10.0 s, resume→ready 5.7 s. The "~0.5 s from the warm pool" figure in the
canonical vision doc describes a mechanism retired on 2026-09-17.

## The heartbeat is an OBSERVATION, and an always-on box never goes down (2026-09-22)

**The heartbeat never existed.** `sandbox-image/sdk/matrx_agent/client.py::heartbeat()`
is written and nothing calls it; the "matrx_agent daemon pings every ~60s" that
aidream's `ensure_default_sandbox` documents as THE liveness signal has never
run. Measured on production: of 273 `sandbox_instances` rows only 9 had EVER
carried a `last_heartbeat_at`, the newest was two days old, and 223 of the 226
live rows had none. So aidream called every box a corpse and cold-created a new
one on each contact (a 364 s create instead of a ~4 s reuse), and the shipped
`heartbeat_extends_ttl` knob governed nothing. Fixed where the truth already
lives: the 60-second liveness reconcile already asks Docker which containers are
alive, so that same UPDATE stamps `last_heartbeat_at` and applies the knob. The
column now means **"the last time the platform OBSERVED this box alive"** — a
stronger signal than a container asserting its own health. Guards:
`tests/test_observed_liveness_is_the_heartbeat.py`.

**Always-on workspaces.** A box carrying `labels->>'always_on' = 'true'` must
stay up; the only honest states are up, or an outage something is repairing.
One predicate, one module (`orchestrator/always_on.py`), four call sites: the
TTL sweep and the retention purge exclude it, `docker run` gives it
`restart_policy=unless-stopped` so it survives a daemon restart, and the
existing 60s reaper tick revives a down one through the orchestrator's OWN
resume path — tier-scoped, newest row per (user, organization), never beside a
live box, capped by `always_on_revive_max_per_pass`, and braked unconditionally
by `always_on_revive_min_interval_seconds` so a box that cannot boot is not
resurrected every minute forever. Every revive logs at WARNING. Guards:
`tests/test_always_on_workspace.py`.

**`stop_reason` has five legal values.** `sandbox_instances_stop_reason_check`
admits only `user_requested | expired | error | graceful_shutdown | admin`;
anything else RAISES and the row keeps its stale status. Two live instances
fixed: the token-issuance liveness stop, and `_wait_for_ready`'s free-text boot
failures (which made the whole save of a FAILED row raise, so it stayed
`creating` with no reason at all). `sandbox_manager.record_boot_failure` is the
chokepoint — canonical reason in the column, the honest sentence in
`config['stop_detail']` and the log. Guard:
`tests/test_stop_reason_is_always_admissible.py`.

## The TTL is an idle ceiling (2026-09-20)

`models.py` and `reaper.py` both described a heartbeat-refreshed ceiling while
`store.update_heartbeat` stamped `last_heartbeat_at` and nothing else, so a box
somebody was working in died on a wall clock. A heartbeat from a LIVE row now
rolls `expires_at` forward by its full `ttl_seconds`, under
`infrastructure.sandbox.heartbeat_extends_ttl` (default on). A terminal row, or
one whose TTL clock never started, is never touched. Guards:
`tests/test_heartbeat_extends_ttl.py`.
