# Co-located AI Dream — the production low-latency plan

Status: **proposal + runbook** · Date: 2026-05-23

The decision (yours, 2026-05-23): instead of building a stripped-down "light
Matrx AI" to bake into every sandbox, **run the full AI Dream once, on the same
local network as the EC2 sandboxes.** Since it runs once (not duplicated into
thousands of boxes), its size doesn't matter — reuse the full AI Dream that
already works, keep the sandbox boxes lean.

---

## Why this works (the principle)

The latency problem is the per-tool-call round trip when the agent loop is far
from the box:

```
Anthropic → AI Dream → Sandbox (exec → result) → AI Dream → Anthropic
                       └──────── slow if this crosses the internet ────────┘
```

Fix: put AI Dream (the loop) on the **same LAN** as the sandbox containers, so
that middle hop is local (sub-millisecond) instead of cross-internet. The only
call that leaves the network is `AI Dream → Anthropic`, once per turn —
unavoidable everywhere.

```
        ┌──────────────── same AWS VPC + same AZ ────────────────┐
        │                                                         │
  Anthropic ⇄  AI Dream (full)  ⇄  EC2 orchestrator  ⇄  sandbox containers
        │      (the loop)          (private IP)          (slim + matrx_agent)
        └─────────────────────────────────────────────────────────┘
```

- **Sandbox boxes stay lean** (`matrx-sandbox:slim` + `matrx_agent`). No brain
  baked in. Fast to spawn — which was the original "smaller, easier to launch"
  goal, satisfied better than putting AI in each box.
- **AI Dream's tools** reach the boxes via the orchestrator's structured tool
  routes (already built; scoped-token auth + `/agent-binding`). The binding's
  `base_url` must use the **internal/private** orchestrator address so traffic
  stays on the LAN.

---

## Cost (honest, verify exact numbers against your AWS account)

| Item | Cost | Notes |
|---|---|---|
| One always-on AI Dream instance, co-located | the main new cost | Sized for AI Dream (FastAPI + provider/Supabase calls). Rough order: a `t3.large`/`m6i.large`-class box ≈ **$60–150/mo** on-demand, less with a savings plan. **Verify against actual AI Dream resource needs.** |
| Same-AZ, same-VPC traffic (AI Dream ↔ orchestrator ↔ boxes) | **$0** | AWS does not charge for private-IP traffic within one AZ. **This is the key lever — keep AI Dream and the sandbox host in the SAME Availability Zone.** Cross-AZ would add ~$0.01/GB each way. |
| Warm pool (2 idle slim containers) | negligible | A little idle CPU/RAM on the existing sandbox host. |
| LLM API calls | unchanged | Same wherever the loop runs. |
| ECR storage for `:slim` | negligible | ~1 GB image. |

**Cheaper alternative to consider:** if the EC2 sandbox host has spare capacity,
run AI Dream **on that same host** (as a container/process) instead of a
separate instance → localhost latency, **no extra instance cost** (maybe a
bigger host). Trade-off: AI Dream competes with sandboxes for host resources.
Good for a single-host setup; a shared nearby instance is better for a fleet.

---

## INFO NEEDED before the exact plan can be finalized

I don't have these and won't guess. A browser agent on your AWS console can
gather #1–#2 in minutes:

1. **The EC2 sandbox fleet's location:** region, VPC id, subnet/Availability
   Zone, and instance type of the box at `54.144.86.132`. (AWS console → EC2 →
   that instance → Details.)
2. **Where prod AI Dream runs today** (`server.app.matrxserver.com`): which
   provider/region? Is it already on AWS, and if so, in the same region as the
   sandbox fleet? (If yes, co-location may just mean "same VPC/AZ" without a new
   deployment.)
3. **AI Dream's resource footprint** (CPU/RAM it needs to run comfortably) — to
   size the co-located instance and firm up the cost.

---

## Runbook — remaining steps, tagged by who does them

### [ME — already done, in matrx-sandbox `main`]
- Slim box, warm pool, memory, scoped-token tool auth, `/agent-binding`.
- Nothing else from me is required for the orchestrator/box side until the
  facts above come back (then: confirm the binding uses the internal address).

### [BROWSER AGENT / YOU — on AWS]
1. **Gather the facts** in "INFO NEEDED" #1–#2.
2. **Provision the co-located AI Dream** in the **same VPC + same AZ** as the
   sandbox fleet (or co-host on the sandbox EC2 if it has headroom):
   - Launch an instance (or ECS service) sized per INFO #3.
   - Security group: allow the AI Dream instance to reach the EC2 orchestrator
     on its port (8000) over the **private** subnet; allow outbound HTTPS (to
     Anthropic + Supabase).
   - Deploy AI Dream there using your existing AI Dream deploy process, with
     the same env it uses in prod (it reads conversations from the same
     Supabase, so no data migration).
3. **Confirm same-AZ** placement (cost + latency lever).

### [AIDREAM OPS — env on the co-located instance]
- Standard AI Dream prod env (Supabase, provider keys, JWT secret) — the same
  it already runs with. Keys-in-environment is the existing model; nothing new.
- (Optional, for the files-bridge "PDFs copied in" flow) set
  `AIDREAM_SANDBOX_SERVICE_TOKEN` to match the orchestrator's
  `MATRX_AIDREAM_SERVICE_TOKEN`.

### [FRONTEND — matrx-frontend, your Vercel deploy]
The handoff flow (also in [CONVERSATION_HANDOFF.md](CONVERSATION_HANDOFF.md)):
1. When a conversation should run in a sandbox: `POST {ec2-orch}/sandboxes/claim`
   `{ user_id, template:"slim", ttl_seconds }` → `{ sandbox_id }`.
2. `POST {ec2-orch}/sandboxes/{id}/agent-binding` → `{ sandbox_id, base_url,
   access_token, root_path }`.
3. Route the chat turn to the **co-located AI Dream** endpoint (not remote
   prod), passing that object as the request's `sandbox` field.
   - **Key detail:** the binding's `base_url` must be the orchestrator's
     **internal** address (private IP / internal DNS), so AI Dream's tool calls
     stay on the LAN. This is set via the orchestrator's `MATRX_PUBLIC_URL`
     (or a dedicated internal-URL setting) on the co-located deployment.

### [ORCHESTRATOR CONFIG — EC2 side]
- Set `MATRX_WARM_POOL_SIZE=2` + `MATRX_WARM_POOL_TEMPLATE=slim` in the EC2
  orchestrator's systemd env (mirrors the hosted setting; see OPERATIONS.md
  "Out-of-repo live settings").
- Ensure `MATRX_PUBLIC_URL` (used to build the `/agent-binding` base_url)
  resolves to an address the co-located AI Dream can reach **privately**. If
  the public URL routes back out to the internet, tool calls won't be LAN-fast
  — this is the single most important wiring detail.

---

## Open question for later: in-box light Matrx AI

Co-location makes this unnecessary for now. The only reasons to revisit a
light-Matrx-AI-in-the-box build later:
- You want boxes that work **fully offline / disconnected** from any AI Dream.
- You want truly **zero** tool latency (in-process, vs. the ~ms LAN hop).

Neither is required for the stated use cases. Documented here so the decision
is explicit, not forgotten.
