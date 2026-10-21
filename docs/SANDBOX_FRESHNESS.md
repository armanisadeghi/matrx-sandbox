# Sandbox freshness: how boxes stay on the current image

Tracker: cf1ab9ab (2026-10-21). Gate: SBX-013 in
`common-docs/systems/architecture/sandboxes/STATE.md` (the former REGISTER).

## Three layers

1. **SDK refresh (live, every box).** At binding, the orchestrator refreshes
   `/opt/sandbox/sdk` and `mtx` in place (`sdk_refresh_on_binding`). This carries
   all agent-facing tooling forward (session brief, repo tidy, image-drift
   report) without a container swap. It cannot change the OS, interpreter or
   system packages.
2. **Self-report (live, every box).** `matrx_agent/image_drift.py` compares the
   image the container was built from (`MATRX_IMAGE_VERSION`) with the image the
   host currently serves (SDK refresh stamp `to_version`). `mtx whoami`,
   `mtx self-update --status` and the session brief print `STALE IMAGE: ...`
   with the concrete stale signals (OS, python3) and whether the box can update
   automatically. Failed SDK refresh trees are pruned to the newest 3.
3. **Automatic container swap (built, OFF).** `orchestrator/auto_update_policy.py`
   decides whether `migrate_all_drifted` may swap a box. It is gated by the
   master switch `infrastructure.sandbox.auto_migrate` (false) and the stage
   knob `infrastructure.sandbox.auto_update_stage` (missing = off). See the module
   docstring for every gate: storage kind, opt-out, detached work, settle /
   spacing / failure hold, preservation manifest, durable ledger.

## Turning it on (operator steps — needs DB access and Arman's sign-off)

1. Seed knobs `auto_update_stage` = `canary`, `auto_update_canary_sandbox_ids`
   (one disposable slim box), soak/settle/spacing/max-failure values.
2. Set `auto_migrate` = true. Watch the ledger and the preservation-check
   results for one target version through the soak.
3. Move to `fleet`. Any preservation mismatch halts that version fleet-wide.

## Boxes that cannot be swapped automatically

A host bind-mounted home (EC2 `development` template,
`persistence_volume=host:*`) is `manual_only`: the engine refuses it before
stopping anything ("home must be one writable named Docker volume"). Arman's
`sbx-7ddc2eb0c364` is one. Updating such a box means moving its home onto a
named volume first, or recreating it after every repo is pushed. Its 2026-09-08
migration had no pre-manifest, so byte preservation was never proven.

## Opt-out

`touch /home/agent/.matrx/auto-update.off` inside the box, or the container
label `matrx.auto_update=off`.
