# Frontend Handoff — running the agent inside a sandbox (end-to-end)

**For:** the matrx-frontend team · **Date:** 2026-05-24 · **Backend status:** live & verified

This is the single doc for wiring the frontend so a user's agent does its real
work — read/write files, run commands, clone + push a repo — **inside a Matrx
Sandbox**, fast, with the user's memory present. Everything behind the FE is
already built, deployed, and tested. This explains exactly what the FE calls.

---

## 1. What's already done (you can rely on it)

- **Sandboxes** spawn in ~0.5s from a warm pool (lean `slim` image) and arrive
  with the user's cross-project memory already inside (`/home/agent/.matrx/memory/`).
- **A co-located AI Dream** runs on the same AWS network as the sandboxes
  (`https://sandbox.matrxserver.com`). It runs the agent loop; its filesystem/
  shell/git tools execute **inside the sandbox over the private LAN** (fast,
  free). This is the whole reason latency is acceptable.
- The orchestrator endpoints (`/claim`, `/agent-binding`) and AI Dream's
  `sandbox` binding are deployed and verified end-to-end on the private path.

**Your job:** three calls — claim a box, get its binding, attach the binding to
the chat turn you send to the co-located AI Dream.

---

## 2. The three endpoints

| Purpose | URL | Auth | Called from |
|---|---|---|---|
| Claim a sandbox | `POST http://54.144.86.132:8000/sandboxes/claim` | `X-API-Key` (orchestrator master key) | FE **backend** (Next.js) |
| Get the agent binding | `POST http://54.144.86.132:8000/sandboxes/{id}/agent-binding` | `X-API-Key` | FE **backend** |
| Send the chat turn | `POST https://sandbox.matrxserver.com/chat` (or `/agents/{id}`) | your existing AI Dream auth | the **browser** (or FE backend) |

> `54.144.86.132:8000` is the EC2-tier orchestrator (where these sandboxes
> live). You already route to it via the existing tier mechanism
> (`lib/sandbox/orchestrator-routing.ts`, `MATRX_ORCHESTRATOR_URL` /
> `MATRX_ORCHESTRATOR_API_KEY`). Reuse that — no new orchestrator client needed.

---

## 3. The flow (step by step)

```
1. FE backend → POST {orchestrator}/sandboxes/claim
      headers: X-API-Key: <orchestrator master key>
      body:    { "user_id": "<auth user uuid>", "template": "slim", "ttl_seconds": 3600 }
   → 201 { "sandbox_id": "sbx-…", "status": "ready", ... }

2. FE backend → POST {orchestrator}/sandboxes/{sbx-…}/agent-binding
      headers: X-API-Key: <orchestrator master key>
      body:    {}                                  # optional: {scopes, ttl_seconds}
   → 200 {
        "sandbox_id":   "sbx-…",
        "base_url":     "http://172.31.91.106:8000/sandboxes/sbx-…",   # PRIVATE — correct
        "access_token": "…",                                          # short-lived, sandbox-scoped
        "root_path":    "/home/agent"
      }

3. Browser → POST https://sandbox.matrxserver.com/chat
      (your normal chat request body) PLUS:
      "sandbox": {
        "sandbox_id":   "sbx-…",
        "base_url":     "http://172.31.91.106:8000/sandboxes/sbx-…",
        "access_token": "…",
        "root_path":    "/home/agent"
      }
   → AI Dream runs the turn; its fs/shell/git tools execute INSIDE sbx-… ;
     streams back as your chat normally does.
```

- The `sandbox` object in step 3 is **the step-2 response verbatim** — no
  reshaping. AI Dream's `SandboxBindingRequest` matches it exactly.
- `base_url` is intentionally a **private `172.31.x.x` address**. The FE never
  calls it — only AI Dream (which is on the same private network) does. Pass it
  through untouched.
- `access_token` is short-lived and sandbox-scoped (not the master key) — safe
  to hand to the browser; that's its designed use.

---

## 4. ⚠️ One required AI Dream change for "continue an existing conversation"

The `sandbox` field is currently accepted on:
- `POST /chat`, `POST /manual` (`aidream/api/routers/chat.py`)
- `POST /agents/{agent_id}`, `POST /agent/{agent_id}` (`aidream/api/routers/agents.py`)

It is **NOT** yet accepted on `POST /conversations/{conversation_id}` (the
continue-an-existing-conversation endpoint). So:

- **New chat / new agent run in a sandbox** → works today via `/chat` or `/agents/{id}`.
- **Continue an EXISTING conversation in a sandbox** → needs a ~2-line aidream
  addition: add the field to the conversations request model and set the
  metadata, mirroring `chat.py`:
  ```python
  # in the conversations request model:
  sandbox: SandboxBindingRequest | None = None
  # in the handler, before running (mirror chat.py:230):
  if request.sandbox is not None:
      new_metadata["active_sandbox"] = request.sandbox.model_dump(exclude_none=True)
  ```
  This goes in the **`AI-Matrix-Engine/aidream`** repo (what `sandbox.matrxserver.com`
  deploys from) and ships via its own deploy. Until then, route sandbox-bound
  turns through `/chat`/`/agents/{id}`.

(If you only ever start a fresh conversation when entering a sandbox, you can
ship without this. It's required only for "take the conversation I'm already in
and continue it inside a box.")

---

## 5. Per-user memory (already live; optional UI)

The agent inside the box automatically gets the user's cross-project memory
(notes/preferences/project facts) at `/home/agent/.matrx/memory/`, and edits it
makes are captured back when the box is torn down. **No FE work is required for
the agent to have memory.**

Optional: a UI to let users view/edit their memory. It's a Supabase table
(`user_memory`, RLS-scoped) in the same Matrx Main project you already use, so
read/write it Supabase-direct. Full recipe: **`docs/MEMORY_API.md`** (also
`regenerate Supabase types` — the table is new).

---

## 6. Env vars the FE needs

```
MATRX_ORCHESTRATOR_URL=http://54.144.86.132:8000        # already have this
MATRX_ORCHESTRATOR_API_KEY=<EC2 orchestrator master key> # already have this
MATRX_SANDBOX_AIDREAM_URL=https://sandbox.matrxserver.com # NEW — where sandbox-bound chats go
```

For sandbox-bound conversations, send the chat turn to `MATRX_SANDBOX_AIDREAM_URL`
instead of the default AI Dream host. Normal (non-sandbox) conversations are
unchanged.

---

## 7. Caveats / notes

- **AI Dream auth is unchanged.** The co-located AI Dream at
  `sandbox.matrxserver.com` runs the same app against the same Supabase as your
  normal AI Dream backend, so the **same user auth (JWT) you already send works
  here** — you're only changing the host for sandbox-bound turns, not the auth.
- **CORS:** the browser calls `sandbox.matrxserver.com` cross-origin. AI Dream's
  CORS allow-list must include your frontend origin (the same one it already
  allows for the main AI Dream host). If you get a CORS error in the browser,
  that allow-list on the co-located server is the fix (an AI Dream/ops config,
  not FE code).
- **Cloudflare bot protection:** `sandbox.matrxserver.com` is behind Cloudflare.
  Requests with a real **browser** User-Agent pass (HTTP 200). A non-browser UA
  (e.g. server-side Node `fetch` from Vercel SSR) may get a managed challenge
  (403). The documented path (browser → AI Dream) is fine. If you must call it
  server-side, ask infra to add a Cloudflare WAF rule that skips Bot
  Fight/Managed Challenge for `http.host eq "sandbox.matrxserver.com"`.
- **Speed:** `/claim` returns a pre-warmed box in ~0.5s. If the pool is empty it
  transparently cold-creates (slower but works) — no special handling needed.
- **Template:** use `"slim"` for the lean coding box (clone repo, run tools,
  push a branch). `"aidream"` is the heavy full-environment box if ever needed.
- **Lifecycle:** boxes auto-expire at `ttl_seconds` (the reaper tears them down,
  preserving the user's volume where applicable) and can be resumed via
  `POST /sandboxes/{id}/resume`. Memory persists regardless.

---

## 8. FE task checklist

- [ ] Add `MATRX_SANDBOX_AIDREAM_URL` to the env.
- [ ] On "work in a sandbox": backend calls `claim` → `agent-binding` (reusing the existing orchestrator client/key).
- [ ] Attach the binding object as the `sandbox` field on the chat turn, sent to `MATRX_SANDBOX_AIDREAM_URL`.
- [ ] (If continuing existing conversations in a box) coordinate the §4 aidream change.
- [ ] (Optional) memory viewer per `docs/MEMORY_API.md`.

---

## 9. Reference docs (for depth, not required)

- `docs/CONVERSATION_HANDOFF.md` — the handoff model + contracts.
- `docs/COLOCATED_AIDREAM.md` — the AWS topology + why tool calls are private/fast.
- `docs/MEMORY_API.md` — the memory store + UI recipe.
- `docs/EC2_LIGHTWEIGHT_BOX.md` — what the `slim` box is.
