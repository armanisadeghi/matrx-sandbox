# Vision / North Star — per-user permanent sandboxes on in-region S3

Date: 2026-05-24 · Owner: founder (info@aimatrx.com)

This is the durable "where this is going" so any agent picking up cold knows the
goal behind the recent work. Keep it current.

## The goal

**Every user gets their own permanent Matrx sandbox.** Coders / power users get
more (bigger, or additional boxes). The killer capability is that the agent
inside a user's sandbox has **cheap, fast, in-region access to that user's own
S3 files** — so an agent can work over the user's documents/data natively
(grep, read, process PDFs/images, etc.) without slow cross-region transfers or
per-call egress cost.

Rollout: **pilot with the first ~20 users**, each given a permanent sandbox;
expand from there. Coders who need more get more.

Possible future: use S3 to **tear down and bring back additional per-user
buckets/boxes** on demand (spin up extra storage/workspaces, park them in S3,
restore when needed) — an extension of the same in-region-S3 economics.

## Why region consolidation is the enabling step

Today user files (and other infra) live across regions (us-east-2 / us-west-2).
The sandboxes + the co-located AI Dream run in **us-east-1 (AZ us-east-1d)**.
Cross-region S3 access from a sandbox is **slow and costs egress**. So:

> Consolidate everything — **including the user S3 file buckets** — into
> **us-east-1**, same region/AZ as the sandbox host and AI Dream. Then a
> sandbox's access to the user's files is in-region: **cheap (no cross-region
> egress), fast (low latency), and simpler.**

That is the whole reason for the current S3 migration
([../../matrx-ship/FILE_MOVE_TASK.md](../../matrx-ship/FILE_MOVE_TASK.md)) and
why the AI Dream backend was moved onto an EC2 box in us-east-1.

## How this builds on what's already shipped

The per-user permanent sandbox + S3-file-access flow sits directly on top of the
machinery already built (all live/verified on the orchestrator):

- **Warm pool + `/sandboxes/claim`** — a box is ready in ~0.5s.
- **Cross-project per-user memory** (`user_memory` → `.matrx/memory/`) — the
  agent in the box already knows the user/projects/preferences.
- **`/agent-binding` + scoped-token tool routes** — the agent's fs/shell/git
  tools execute *inside* the box (the "agent works in the sandbox" path).
- **Co-located AI Dream** (`sandbox.matrxserver.com`, us-east-1) — the agent
  loop runs on the same LAN as the boxes, so tool calls are fast.
- **EC2 control plane** (Server Manager `FLEET_HOSTS` + `/api/hosts/*` + the
  planned Hosts UI in [../../matrx-ship/EC2_HOSTS_UI_TASK.md](../../matrx-ship/EC2_HOSTS_UI_TASK.md))
  — manage the boxes + launch agents into them from one UI.

What's NEW for this vision (the next layer):
1. **Permanent per-user sandboxes** — the lifecycle already supports "never
   expires" (huge `ttl_seconds`); needs the per-user provisioning flow + the
   pilot-20 rollout.
2. **In-region user-file access** — once the S3 migration lands, wire the
   sandbox's file tools / cloud-files to the user's us-east-1 buckets
   (cheap/fast). The AI Dream files bridge ([AIDREAM_INTEGRATION.md](AIDREAM_INTEGRATION.md))
   is the surface for this.

## Current transition status (update me)

- **S3 region migration** (us-east-2 → us-east-1, same names): IN PROGRESS via
  `FILE_MOVE_TASK.md`, run on `matrx-python-server` through the Manager's SSM.
  STEP 1 (sync source→temp) re-running after the instance role got the S3
  policy (incl. object-tagging perms). STEP 3 cutover is DESTRUCTIVE — only with
  explicit per-bucket confirmation.
- **EC2 Hosts UI** (terminal + agent access in the Manager): task written,
  not yet built — `EC2_HOSTS_UI_TASK.md`.

See [/root/.claude memory `project-matrx-sandbox-state`] for the running ground
truth of what's live vs pending.
