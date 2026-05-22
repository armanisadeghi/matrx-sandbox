# EC2 Lightweight Coding Box — Spec

Status: **proposal** · Author: lifecycle work follow-up · Date: 2026-05-22

> The "big big missing thing": a minimal sandbox you can launch from a chat
> window, that clones a repo, runs tools (grep/find/tests/build), talks to the
> server API, and pushes to a new git branch. Fast cold start. Plus a secondary
> file-processing use case (PDFs/images copied in, agent works, done).

This spec is deliberately opinionated about **one decision that changes
everything else**: the lightweight box's persistence model is **git, not a
volume and not S3**. Everything below follows from that.

---

## 1. The core principle: git is the persistence

| | Hosted tier | Current EC2 tier | **Lightweight box** |
|---|---|---|---|
| "The data" lives in | per-user Docker volume (`matrx-user-<uid>`) | `s3://…/users/{uid}/hot/` synced to `/home/agent` | **the git remote** |
| On boot | mount volume | `aws s3 sync` DOWN (5–30s, [hot-sync.sh:53](../sandbox-image/scripts/hot-sync.sh#L53)) | `git clone --depth` (1–5s) |
| On teardown | keep volume | `aws s3 sync` UP + FUSE flush | nothing — work was pushed |
| Output of a run | long-lived workspace | long-lived workspace | **a pushed branch / a PR** |
| Lifetime | long | 2h default | minutes; ephemeral by design |

Because the box is stateless between runs, we can **delete** the entire S3
hot/cold machinery from its image and entrypoint. That removes the dominant
cold-start cost (the S3 hot-sync) *and* the two biggest size drivers (AWS CLI,
and — since there's no browser work — Chromium). The result is a box that is
both small and fast, which is exactly what "launch from a chat window"
requires.

If the agent doesn't `git push`, the work is gone. That is the intended
contract, not a bug: ephemeral boxes that leak state are how you end up with 17
orphaned sandboxes again.

---

## 2. What's in the image

Today's `:core` image is ~2.9 GB ([Dockerfile](../sandbox-image/Dockerfile)),
broken down roughly:

| Component | ~Size | Coding box? |
|---|---|---|
| Ubuntu 22.04 base | 500 MB | ✅ keep |
| git, ripgrep, fd, jq, curl, openssh-client, build-essential, poppler-utils | 150 MB | ✅ keep — this is the coding toolkit |
| Python 3.11 + (httpx, pydantic, requests, pyyaml, numpy, pandas…) | 200 MB | ✅ keep |
| matrx_agent SDK + scripts | 5 MB | ✅ keep — the capability surface |
| **Chromium + Playwright** | **800 MB** | ❌ cut — no browser tasks |
| **AWS CLI v2** | **500 MB** | ❌ cut — no S3 sync |
| **mountpoint-s3 (FUSE)** | 100 MB | ❌ cut — no cold tier |
| **Node.js 20** | **250 MB** | ⚠️ decision (see §7) |

**Target: a `Dockerfile.slim` of ~700 MB–1 GB** (≈ Ubuntu + Python + git
toolchain + SDK), or ~950 MB–1.25 GB if Node stays. That's a 2–4× smaller pull
than `:core`, which is most of the cold-start win on a warm instance.

Build it as a sibling to the existing variants
([Dockerfile.aidream](../sandbox-image/Dockerfile.aidream) is the precedent):
`sandbox-image/Dockerfile.slim`, tagged `matrx-sandbox:slim`.

### Secondary use case: file processing (PDFs/images)
`poppler-utils` (PDF→text/image) is already in the base toolkit and stays.
Add `Pillow` (image ops) and optionally `tesseract-ocr` — both cheap. Files
come in via the existing `POST /fs/upload` (multipart) or
`POST /fs/write` (base64) endpoints; results come out via `GET /fs/download`
(raw or zip). No new capability needed — it's the same box with an input
folder instead of a git URL.

---

## 3. The clone → work → push flow

Every primitive below **already exists** in `matrx_agent`
([sdk/matrx_agent/api/](../sandbox-image/sdk/matrx_agent/)), proxied through the
orchestrator with API-key auth. The flow is just a sequence of existing calls:

```
1. POST /sandboxes                      { tier:"ec2", template:"slim",
                                          ttl_seconds: 1800 }
   → sbx-… (ready in seconds; see §4)

2. POST /sandboxes/{id}/credentials     { kind:"github", token:"<PAT>" }
   → writes the git credential helper (credentials.py:17)

3. POST /sandboxes/{id}/git/clone       { url, dest:"repo", depth:1, branch }
   → /home/agent/repo  (git.py:57)

4. … agent works …
   POST /sandboxes/{id}/exec/stream     { command:"pnpm test", cwd:"repo" }     (SSE)
   POST /sandboxes/{id}/search/content  { query, cwd:"repo" }                   (ripgrep)
   PUT  /sandboxes/{id}/fs/write        { path, content }                       (edits)
   POST /sandboxes/{id}/git/diff|add|commit

5. POST /sandboxes/{id}/git/branch      { action:"create", name:"agent/fix-x" }
   POST /sandboxes/{id}/git/push        { branch:"agent/fix-x" }
   → branch on the remote = the deliverable

6. POST /sandboxes/{id}/credentials/revoke   (drop the token)
   DELETE /sandboxes/{id}                     (or let it expire — reaper tears it down)
```

Note step 6 leans on the lifecycle we just shipped: an ephemeral box doesn't
even need an explicit delete — set a short `ttl_seconds` and the reaper
gracefully reaps it. Since there's no volume/S3 to flush, teardown is instant.

**One ergonomics add worth building:** a single `POST /sandboxes/quick-task`
convenience endpoint on the orchestrator that does create + credentials + clone
in one call and returns `{ sandbox_id, workspace }`, so the chat window fires
one request instead of three. Thin wrapper over the above; optional.

---

## 4. Cold-start reality — the honest constraint

This is the constraint you asked me to surface. "Launch from a chat window"
implies single-digit seconds. Whether we hit that depends entirely on **where
the image and the instance already are**:

| Scenario | Time to "ready" | Why |
|---|---|---|
| Warm instance, image pre-pulled, slim image | **~3–8s** | container start + `git clone --depth 1` |
| Warm instance, image NOT cached | +30–90s | `docker pull` of ~1 GB from ECR |
| Cold instance (must launch EC2) | +60–180s | EC2 boot + Docker + pull |

Conclusions:

1. **The agent loop / container start is not the bottleneck — image presence
   and instance presence are.** A slim image only helps the "not cached" row;
   it does nothing for a cold instance.
2. **To make "launch from chat" real, the image must be resident on a running
   instance before the request arrives.** Two ways:
   - **(A) Warm pool** — keep N EC2 instances up with `matrx-sandbox:slim`
     pre-pulled (or baked into the AMI). Launch = `docker run` on a warm host.
     Fastest; costs idle instance-hours.
   - **(B) Baked AMI + on-demand instance** — image in the AMI so no pull, but
     you still pay EC2 boot (~60–90s). Cheaper; not "chat-fast."
   - Current EC2 tier is effectively (B) without the baked image, hence slow.
3. The slim image is necessary but **not sufficient** for chat-speed. The warm
   pool (A) is the lever. Recommendation: **a small warm pool of 1–2 slim
   instances**, sized to demand, is the cheapest path to chat-speed; everything
   else is a pull or a boot.

This is the decision I need from you (see §8): are we willing to keep a small
warm pool, or is ~60–90s "click and wait" acceptable for v1?

---

## 5. Honest limitations vs. the hosted tier

| Capability | Hosted tier | Lightweight box |
|---|---|---|
| Persistence across sessions | ✅ volume survives | ❌ only what's pushed to git |
| Large/!git data (datasets, media) | ✅ `/data/cold` FUSE | ❌ no cold tier; container disk only |
| Browser / Playwright | ✅ | ❌ cut |
| aidream template | ✅ | ❌ (use hosted) |
| Long-lived editor session | ✅ | ❌ ephemeral by design |
| Internal-network reach (shared PG, MCP) | ✅ | ⚠️ EC2 is public-only |
| Fast launch from chat | ⚠️ slower (heavy image) | ✅ (with warm pool) |
| Cost per task | higher (long-lived) | lower (seconds, torn down) |
| Best for | editor sessions, big workloads | agent runs: clone → fix → push; file jobs |

The two tiers are complementary, not competing: hosted = workspace,
lightweight = task runner.

---

## 6. How this maps to your 4-layer device-runtime vision

The lightweight box is the **substrate** for that vision, delivering Layer 1
today and leaving room for Layer 3 later:

- **Layer 1 — Capability surface (the hands):** ✅ this is `matrx_agent`. fs,
  process, shell, git, search, ports. Shipping it on a slim image *is* "hands
  on EC2."
- **Layer 2 — Tool dispatch + safety:** today the orchestrator boundary
  provides auth (`X-API-Key`) + an audit trail (request-logging middleware).
  The allowlist/policy engine + approval gates are **not built** — they'd live
  here. For an ephemeral, git-scoped box the blast radius is already small
  (it can only push to branches the token allows), which buys time on this.
- **Layer 3 — Agent loop (provider clients, loop, local SQLite state):**
  **not on the box today.** Today the loop runs in AI Dream / the chat and
  drives the box via the orchestrator proxy. Your vision moves the loop onto
  the device. The slim image should be built to *allow* that later — leave room
  for provider SDKs + a SQLite file under `/home/agent/.matrx/` — but v1 keeps
  the loop remote. That's the smaller, shippable step.
- **Layer 4 — Control plane connection:** the orchestrator + cloud-files bridge
  already are this. AI Dream pushes tasks/config; the box streams events up via
  heartbeat + (optionally) the cloud-files watcher.

So the staged path matches your "control plane, not data plane" framing:
**v1 = hands on EC2, loop remote** (orchestrator-mediated). **v2 = move the
loop onto the box** (Layer 3 on-device), at which point AI Dream becomes pure
control plane and the box runs autonomously between check-ins. v1 doesn't
preclude v2; it's the substrate for it.

Heuristic check ("if removing the network would break the primary job, it
belongs on the device"): for v1 the primary job is clone→work→push, which
*does* need the network (git remote + LLM API), so a remote loop is fine. For
the autonomous-device vision, the loop must survive flaky links — that's the v2
trigger to push Layer 3 down.

---

## 7. Build decisions (resolved 2026-05-22)

1. **Node.js in the slim image: YES — one slim with Node.** Single
   `matrx-sandbox:slim` carries Python 3.11 + Node 20, handling both Python and
   JS/TS repos. ~250 MB on top of the base is cheap next to the ~1.3 GB cut.
2. **Cold-start strategy: WARM POOL (1–2 instances).** Keep 1–2 EC2 instances
   up with `matrx-sandbox:slim` resident; launch = `docker run` on a warm host
   → ~3–8s, true chat-speed. Accepts idle instance-hours as the cost of the UX.
   Requires a small placement controller (§8 step 5).
3. **`quick-task` convenience endpoint:** build it (§8 step 6) — one chat-side
   call instead of three is worth the thin wrapper.

---

## 8. Build plan (once §7 is decided)

Backend, in dependency order:

1. **`sandbox-image/Dockerfile.slim`** — Ubuntu + Python 3.11 + git/ripgrep/fd/
   poppler/Pillow (+ Node per §7-1) + the matrx_agent SDK. No Chromium, no AWS
   CLI, no FUSE.
2. **`sandbox-image/scripts/entrypoint-slim.sh`** — start `matrx_agent` on
   :8000 + sshd; **skip** hot-sync, cold-mount, cloud-files. (Model on
   [entrypoint.sh](../sandbox-image/scripts/entrypoint.sh) minus steps [1/5],
   [2/5], [4.6/5].) This alone removes the 5–30s S3 wait from boot.
3. **Register the template** — add `slim` to
   [routes/templates.py](../orchestrator/orchestrator/routes/templates.py)
   `_TEMPLATE_IMAGE_OVERRIDES` → `matrx-sandbox:slim`, and to the `GET /templates`
   list.
4. **EC2 image delivery** — extend
   [.github/workflows/deploy.yml](../.github/workflows/deploy.yml) to build +
   push `:slim` to ECR alongside `:core`; pre-pull it on the instance(s) in the
   SSM step (and/or bake into the AMI for the warm pool).
5. **(If warm pool)** — a small controller that keeps N instances up with the
   slim image resident; the orchestrator places `tier:ec2, template:slim`
   creates onto a warm host.
6. **(Optional)** `POST /sandboxes/quick-task` convenience endpoint.

Frontend: a "Run a coding task" entry that collects repo URL + branch + GitHub
token (reuse `features/agent-connections/` token plumbing) and fires the §3
flow. Mostly already covered by the existing sandbox API proxy work.

---

## 9. What we are NOT building (v1)

- On-device agent loop / provider clients (Layer 3) — v2.
- Policy engine + approval gates (Layer 2) — deferred; ephemeral git-scoped
  blast radius is small.
- Cold storage / large-data support — that's the hosted tier's job.
- Browser automation — hosted/`:core`.
- Public preview URLs — separate Traefik work.
