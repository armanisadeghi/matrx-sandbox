# Co-located AI Dream — end-to-end solution (the production path)

Status: **wiring in progress** · Date: 2026-05-23

The agent runs inside a lean sandbox, fast, because the brain (full AI Dream)
now sits on the **same AWS network** as the sandboxes. This doc is the single
source of truth for how it fits together, what's done, and the exact remaining
steps — split by who runs them.

---

## The real topology (your 2026-05-23 provisioning)

```
            ┌──────────── AWS us-east-1, AZ us-east-1d (same AZ = free, sub-ms) ─────────────┐
            │                                                                                 │
 Anthropic ⇄  matrx-python-server (full AI Dream)        matrx-sandbox-host-dev               │
   (internet) │  i-0241f4fee60fb02f6                       i-084f757c1e47d4efb                  │
            │  priv 172.31.83.75 : 8000   ──tool calls──▶  EC2 orchestrator (priv IP : 8000)  │
            │  pub  54.166.106.252          (private LAN)   └─▶ slim sandbox containers        │
            │                                                    (matrx-sandbox:slim)          │
            └─────────────────────────────────────────────────────────────────────────────────┘
                         ▲                                          ▲
                         │ chat turn (HTTPS, public)                │ claim + agent-binding (HTTPS, public)
                         │                                          │
                    matrx-frontend (Vercel) ───────────────────────┘
```

- **AI Dream → sandbox tool calls ride the private LAN** (same AZ, SG-to-SG already open) — this is the latency fix.
- **AI Dream → Anthropic** is the only off-network call, once per turn (unavoidable everywhere).
- **Sandbox boxes stay lean** (`matrx-sandbox:slim` + `matrx_agent`).

---

## End-to-end request flow

1. **FE → orchestrator (public):** `POST /sandboxes/claim {user_id, template:"slim", ttl_seconds}` → a warm box in ~0.5s, with the user's memory hydrated in.
2. **FE → orchestrator (public):** `POST /sandboxes/{id}/agent-binding` → `{ sandbox_id, base_url, access_token, root_path }`. **`base_url` is the orchestrator's PRIVATE address** (`http://172.31.x.x:8000/sandboxes/{id}`) because we set `MATRX_INTERNAL_URL` on the EC2 orchestrator.
3. **FE → AI Dream (public HTTPS):** send the chat turn to the **co-located AI Dream** (`matrx-python-server`), passing that object as the request's `sandbox` field.
4. **AI Dream runs the loop.** Its filesystem/shell/git tools call `base_url` (the orchestrator's private IP) with the scoped token → orchestrator → container. **All on the private LAN.**
5. **AI Dream → Anthropic** each turn. Results stream back to the FE.

---

## What's DONE (me — in matrx-sandbox `main`)

- Slim box, warm pool + `POST /sandboxes/claim`, per-user memory, scoped-token auth on the tool routes, `POST /sandboxes/{id}/agent-binding`.
- **New:** `MATRX_INTERNAL_URL` setting — `/agent-binding` now hands AI Dream the **private** orchestrator address so tool calls stay on the LAN. (Falls back to `MATRX_PUBLIC_URL` if unset, so the hosted tier is unchanged.)
- All pushed to `main`. The EC2 orchestrator picks this up on its next deploy (push to `main` → GHA → SSM to the sandbox host).

## Remaining steps — exact commands, by who

### [ME — done] 1. Orchestrator wiring

The orchestrator now **auto-detects its own EC2 private IP** (instance metadata)
and uses it for the `/agent-binding` `base_url`, so AI Dream's tool calls ride
the private LAN with **no operator config**. Shipped to `main` → deploys to the
EC2 orchestrator. Verified live: EC2 orchestrator is on v0.3.0 with `/claim` +
`/agent-binding`; hosted falls back to the public URL.

Optional (speed only — `/claim` cold-creates fine without it): enable the warm
pool on EC2 via Session Manager on `matrx-sandbox-host-dev`:
```bash
sudo mkdir -p /etc/systemd/system/matrx-orchestrator.service.d
printf '[Service]\nEnvironment=MATRX_WARM_POOL_SIZE=2\nEnvironment=MATRX_WARM_POOL_TEMPLATE=slim\n' \
  | sudo tee /etc/systemd/system/matrx-orchestrator.service.d/warmpool.conf
sudo systemctl daemon-reload && sudo systemctl restart matrx-orchestrator
```
(`MATRX_ACCESS_TOKEN_SECRET` must be set on the EC2 orchestrator for tokens —
it already is if `/agent-binding` returns 200 rather than 503.)

### [BROWSER AGENT / YOU — AWS] 2. Verify the private path AI Dream → orchestrator

From **`matrx-python-server`** (the AI Dream box) Session Manager, confirm it can reach the orchestrator privately:

```bash
curl -s http://172.31.<sandbox-host-priv>:8000/health   # use the PRIV from step 1
# Expect the orchestrator health JSON. If it hangs/refuses, the SG-to-SG rule
# needs port 8000 opened from the AI Dream SG to the sandbox-host SG.
```

### [BROWSER AGENT / YOU — AWS] 3. Give AI Dream a public HTTPS endpoint for the FE

The FE (Vercel) can't call a bare `http://54.166.106.252:8000`. nginx is already
installed on `matrx-python-server`; put a domain + TLS in front (e.g.
`aidream-sbx.<yourdomain>` → nginx :443 → `127.0.0.1:8000`), and a DNS A record
→ `54.166.106.252`. That HTTPS URL is what the FE uses in step 3 of the flow.

### [FRONTEND — matrx-frontend, your Vercel deploy] 4. The three-call handoff

For a conversation that should run in a sandbox:
1. `POST {ec2-orchestrator}/sandboxes/claim` → `{ sandbox_id }`
2. `POST {ec2-orchestrator}/sandboxes/{id}/agent-binding` → binding object
3. Send the chat turn to the **co-located AI Dream HTTPS URL** (step 3 above), with the binding as the request's `sandbox` field.

The FE never calls `base_url` itself — it just forwards the binding to AI Dream, which uses it for tools. Full shapes in [CONVERSATION_HANDOFF.md](CONVERSATION_HANDOFF.md).

### [VERIFY EC2 ORCHESTRATOR IS ON LATEST CODE]

The handoff needs the orchestrator code that's on `main` (agent-binding, warm pool, internal_url). Confirm the EC2 deploy actually landed it:

```bash
curl -s http://54.166.106.252:8000/health   # AI Dream health (sanity)
# On the sandbox host (Session Manager):
curl -s http://localhost:8000/api-surface | python3 -c "import sys,json;d=json.load(sys.stdin);print('has agent-binding:', any('agent-binding' in r['path'] for r in d['routes']))"
# If False, the matrx-sandbox GHA deploy to the sandbox host is stale —
# trigger it: gh workflow run deploy.yml --repo armanisadeghi/matrx-sandbox
# (and confirm that repo's EC2_INSTANCE_ID secret = i-084f757c1e47d4efb)
```

---

## Verified by me (no need to ask)

- EC2 orchestrator `54.144.86.132` is **live, v0.3.0, tier=ec2**, and already
  serves `/claim` + `/agent-binding` (40 routes). My backend work shipped there.
- AI Dream's deployed code accepts the binding: `aidream/api/routers/chat.py`
  `POST /chat` takes a `sandbox: SandboxBindingRequest` field whose shape
  (`sandbox_id, base_url, access_token, root_path`) is **exactly** what
  `/agent-binding` returns. The two were built to fit.

## Two things only you can confirm/do

1. **The deployed AI Dream repo** is `AI-Matrix-Engine/aidream` (the local clone
   I read is `AI-Matrix-Engine/aidream-current`, v0.1.300 — has the binding).
   Confirm the deployed build includes `aidream/api/sandbox_binding.py` + the
   `sandbox` field on `/chat`. (Versions 299/300 strongly suggest same lineage.)
2. **A public HTTPS endpoint for the co-located AI Dream** (nginx is installed
   on `matrx-python-server`; front `:8000` with a domain + cert) — that URL is
   what the FE routes sandbox-bound chats to.

---

## Cost (confirmed reasonable)

Per your provisioning: the AI Dream box is a `t3.medium` ≈ **$27–38/mo** (Savings Plan / on-demand). Same-AZ private traffic between it and the sandbox host is **$0**. That's the whole added cost — well within reason for the latency win.

---

## Not doing (explicit)

- **Light Matrx-AI-in-the-box** — unnecessary now that the full AI Dream is co-located. Revisit only for fully-offline boxes or truly-zero (in-process) tool latency. Neither is required.
