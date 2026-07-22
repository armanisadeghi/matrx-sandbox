# Sandbox Persistence + Session Resume — Plan

**Status:** Phases 1–3 shipped + verified end-to-end on 2026-04-26. Phases 4–5 deferred.
**Owners:** Sandbox backend (matrx-sandbox), Code editor frontend (matrx-frontend `features/code/`)
**Last updated:** 2026-04-26 — directives applied + audit findings integrated + Phases 1–3 implemented

## What landed today (2026-04-26)

- ✅ **Phase 1: hosted-tier per-user Docker volumes.** Volume `matrx-user-<uid>` mounted at `/home/agent` for hosted-tier sandboxes; survives container destroy. Verified: write file in sandbox A → destroy → create sandbox B for same user → file is there.
- ✅ **Phase 2: in-container session manifest + checkpoint daemon.** `matrx_agent.persistence` module writes `/home/agent/.matrx/session.json` every 5 min and on shutdown; renders `session-report.md` on startup.
- ✅ **Phase 3: git auto-stash on shutdown.** Dirty repos under `/home/agent/` are stashed locally + (when creds work) pushed to `matrx/auto-stash/<ts>` branches.
- ✅ **`/internal/{startup,shutdown,manifest,session-report}` routes** on the in-container daemon — invoked by `shutdown.sh` / `shutdown-local.sh` and lifespan hooks.
- ✅ **`GET /users/{uid}/persistence`** + **`DELETE /users/{uid}/volume`** on the orchestrator.
- ✅ **Migration 003** adds `persistence_volume` column to `sandbox_instances`.

Phases 4 (frontend UX) and 5 (quotas + monitoring) remain — see §6.

---

## Tier purposes — when to use which

The two tiers are **not** redundant. Pick by intent:

| Use case | Tier | Why |
|---|---|---|
| Agent runs (one-shot agent fires + dies) | **EC2** | Default 2h auto-shutdown. S3-backed home dir means subsequent runs see prior agent output without keeping a container alive. |
| Quick scripts / CLI tasks | **EC2** | Fast spin-down. Cost-controlled per task. |
| Long-lived editor sessions (day-to-day dev) | **Hosted** | No auto-shutdown. Larger resource ceilings. Local volume = fast disk. |
| Internal tooling that needs Matrx services | **Hosted** | Direct network access to shared Postgres, MCP servers, agent-envs (EC2 sandboxes are firewalled from internal infra). |
| Anything where the user has > 5 GB of work-in-progress | **Hosted** | EC2's S3 sync round-trip on big working sets is slow; the hosted volume avoids it. |
| Anything Matrx-team-owned that wants to be near our internal services | **Hosted** | Same internal-network rationale. |

User data follows the user **within a tier**. Cross-tier portability (a single home dir that follows a user across both tiers) is a Phase 6 future once we wire S3 sync into the hosted tier — not delivered today.

**The starter pool (`sandbox-1`…`sandbox-5`)** was a quick-test placeholder, never intended for real users. They use a third volume model (per-slot, not per-user) and predate this design. They will be retired once Phase 4 of the frontend UX lands and users have the dynamic-create flow on the editor.

---

## 1. Problem statement (from product)

> When a user wants to "bring an instance back," they don't actually care about restoring the exact same instance. What they really want is to return to the **exact environment** they had before they left.

Two requirements:

1. **Permanent user data folder.** Users should have meaningful persistent storage that automatically restores when a new sandbox is created. This must work *equally* across both sandbox tiers (EC2 and hosted), or it's a leaky abstraction.

2. **Optional full-environment resume.** Most of the time the user doesn't want everything brought back — but in the rare cases when they do, we must offer it. Specifically:
   - Track external resources the user added (cloned repos, downloaded data sets).
   - Capture uncommitted git changes (auto-stash to a known branch).
   - Generate a "what was preserved / what was lost" report so users always know the truth.

Plus the implicit requirement: this must not break the existing EC2-tier flow that 5 active production sandboxes depend on right now.

---

## 2. Verified current state (audited 2026-04-26)

Two tiers exist and persist *very* differently. This is the core gap the plan closes.

### 2.1 EC2 tier (production at `54.144.86.132:8000`)

User-scoped S3 persistence — **basically works.** Per-user prefix model:

| Layer | Path in container | S3 prefix | Behavior |
|---|---|---|---|
| Hot (small/active files) | `/home/agent/` | `s3://matrx-sandbox-storage-prod-2024/users/{user_id}/hot/` | `aws s3 sync` down at startup; sync up at graceful shutdown |
| Cold (large files, x86_64 only) | `/data/cold/` | `s3://…/users/{user_id}/cold/` | mountpoint-s3 FUSE — lazy reads, write-through |

Implementation: [sandbox-image/scripts/hot-sync.sh](../sandbox-image/scripts/hot-sync.sh), [cold-mount.sh](../sandbox-image/scripts/cold-mount.sh), [shutdown.sh](../sandbox-image/scripts/shutdown.sh), [entrypoint.sh](../sandbox-image/scripts/entrypoint.sh). Driven by env vars `S3_BUCKET`, `S3_REGION`, `USER_ID` injected by the orchestrator at create time.

**What it does well:** every successive EC2 sandbox a user creates restores their previous `/home/agent/` (within 30s of startup, depending on data size). Hot-sync excludes only `*.tmp`, `.DS_Store`, `__pycache__/*` — `.git/` directories ARE preserved.

**What's broken or weak:**

| Gap | Severity | Description |
|---|---|---|
| **No SIGKILL safety net** | High | The shutdown handler is `trap … SIGTERM SIGINT`. A force-kill (OOM, host reboot, orchestrator hard-stop) skips sync-up entirely → all unsaved hot-storage changes lost. |
| **30s sync timeout** | Med | `shutdown.sh:22` wraps `hot-sync.sh up` in `timeout 30`. A user with a multi-GB workspace can lose data on shutdown. |
| **No periodic checkpointing** | Med | Sync is only at start + graceful shutdown. If a sandbox runs 8 hours and then crashes, all 8 hours of state are at risk. |
| **No state manifest** | High | Nothing captures *what* the sandbox was — no list of cloned repos, no list of running processes, no shell CWD. Files come back; "what was I doing" doesn't. |
| **No git auto-stash** | High | Uncommitted changes survive only as long as the `.git/` dir + working tree are both synced. If a user did `rm -rf` on a repo just before shutdown, their work-in-progress is gone — no separate auto-stash branch caught it. |
| **No quota / usage visibility** | Med | Users can't see how big their persistent storage is, can't browse it, can't delete from it. Costs are unbounded. |
| **Cold storage exists but unused** | Low | FUSE mount works on x86_64 EC2 but no documented user flow puts files there. |

### 2.2 Hosted dynamic tier (this server's `orchestrator.dev.codematrx.com`)

**Zero persistence.** This is the most urgent gap.

The hosted orchestrator's `create_sandbox` ([sandbox_manager.py:144-173](../orchestrator/orchestrator/sandbox_manager.py#L144-L173)) passes **no volume mounts** to spawned containers. The local entrypoint ([entrypoint-local.sh:14-18](../sandbox-local/scripts/entrypoint-local.sh#L14-L18)) explicitly *skips* S3 sync. The shutdown trap ([entrypoint-local.sh:107-119](../sandbox-local/scripts/entrypoint-local.sh#L107-L119)) just kills daemons — no data flush.

**Result:** every `docker rm` on a hosted sandbox loses every byte the user touched. If we ship the editor today and tell users to pick "hosted" tier, their work disappears the moment they stop the sandbox.

### 2.3 Hosted starter pool (`sandbox-1` … `sandbox-5`)

A different, *third* persistence model: each container has a named Docker volume (`sandbox-1-home`, `sandbox-2-home`, …) mounted at `/home/agent/`. Volumes survive container restart but are tied to the **container slot**, not the **user**. If two users use sandbox-1, they share state. This is fine for the demo pool but cannot generalize.

### 2.4 Frontend reality

[features/code/SYSTEM_STATE.md](../../matrx-frontend/features/code/SYSTEM_STATE.md), [SandboxesPanel.tsx](../../matrx-frontend/features/code/views/sandboxes/SandboxesPanel.tsx), [use-sandbox.ts](../../matrx-frontend/hooks/sandbox/use-sandbox.ts):

- **Zero UI for persistence.** No "your previous data," no quota, no session history, no "resume" toggle on create.
- `sandbox_instances` schema already has the columns we need: `user_id`, `project_id`, `organization_id`, `hot_path`, `cold_path`, `config (JSONB)`, `deleted_at`. **Per-user identity is already solid.**
- Wishlist §3.3.1 sketches a snapshot/restore design but it's a P2 future, and it's *instance-scoped* (snapshot of a sandbox) — which doesn't match the user's stated mental model.

The user's mental model is right and the wishlist's is wrong: persistence should be **user-scoped, automatic, and continuous**, not instance-scoped and explicit. We follow the user's framing.

---

## 3. Goals + non-goals

### Goals

1. **Tier parity.** Both tiers offer the same persistence guarantees. A user shouldn't have to know what "tier" means to keep their work.
2. **User-scoped persistence.** "My home directory" follows the user across sandboxes — never tied to a specific container.
3. **Crash-safe.** Survives SIGKILL, host reboot, orchestrator restart. Periodic checkpointing, not just graceful shutdown.
4. **Truthful.** Every restore generates a session report listing exactly what was restored and what was *not* (e.g. running processes, env vars, transient state). Users always know the truth.
5. **Git-aware.** Uncommitted work in any repo under `/home/agent/` survives shutdown via auto-stash + branch push (when creds allow).
6. **Bounded.** Per-user disk quota; visible usage; one-click cleanup.
7. **Doesn't break EC2 today.** The 5 active production sandboxes keep working through every phase.

### Non-goals (explicit)

1. **Full container snapshots (CRIU).** Out of scope — not worth the operational complexity for our use case.
2. **Process / shell-state resume.** We capture a manifest of running processes for the session report but don't restart them on resume. (The wishlist §3.2.4 background-exec story can layer on top later.)
3. **Cross-tier data portability in v1.** EC2 and hosted have separate persistence namespaces initially. We can unify later by making S3 authoritative for both.
4. **Editor-state resume (open tabs, cursor positions).** That's the editor's job — orthogonal to this plan. The editor already persists via Redux + `code_files`/`prompt_apps` — see [SYSTEM_STATE.md §1.7](../../matrx-frontend/features/code/SYSTEM_STATE.md).
5. **Multi-user shared persistent storage.** Wishlist §3.3.2 — out of scope here.

---

## 4. Design

### 4.1 The persistence contract (what we promise users)

A clear, testable contract — written from the user's POV:

> Anything you save under `/home/agent/` (including `.git/` directories) is preserved across sandbox sessions. We sync your home directory to durable storage every 5 minutes and on graceful shutdown. If your sandbox is force-killed, you may lose up to ~5 minutes of work.
>
> Uncommitted work in any git repository under `/home/agent/` is automatically stashed to a `matrx/auto-stash/{timestamp}` branch on shutdown and restored on next session.
>
> Anything outside `/home/agent/` (root filesystem, system services, environment variables set in your shell, running processes) is **not** preserved. We tell you exactly what was lost in the session report you see when you open a new sandbox.
>
> Your data is per-user. Different sandboxes you create see the same `/home/agent/`. Other users never see it.

Make this user-visible in the editor (Settings → Data, plus a one-time onboarding tooltip).

### 4.2 The storage layers

```
┌──────────────────────────────────────────────────────────────────────┐
│                     /home/agent/  (in-container)                     │
│                                                                       │
│   /home/agent/                                                        │
│      .matrx/                ← session manifest, report, runtime state │
│         session.json        ← repos + processes + cwd at shutdown    │
│         session-report.md   ← human-readable "what we restored"      │
│         locks/              ← in-progress operation flags            │
│      <user files & repos>                                             │
└─────────────┬────────────────────────────────────────┬───────────────┘
              │                                        │
        eager sync                              eager sync
        (S3 on EC2,                             (volume mount on hosted,
         volume on hosted)                       optional S3 backup)
              │                                        │
              ▼                                        ▼
┌──────────────────────────────────────┐  ┌────────────────────────────┐
│ EC2 tier:                            │  │ Hosted tier:               │
│ s3://prod-bucket/users/{uid}/hot/    │  │ docker vol matrx-user-{uid}│
│ s3://prod-bucket/users/{uid}/cold/   │  │   mounted at /home/agent   │
│   (FUSE, large files)                │  │ + optional async S3 backup │
│                                       │  │   (off by default for v1)  │
└──────────────────────────────────────┘  └────────────────────────────┘
```

### 4.3 Identity → storage location

Single function in the orchestrator: `resolve_user_storage(user_id, tier) -> StorageLocation`

```python
# orchestrator/orchestrator/storage_layout.py  (new)
@dataclass
class StorageLocation:
    tier: Literal["ec2", "hosted"]
    volume_name: str | None      # hosted tier
    s3_bucket: str | None        # ec2 tier (and optional hosted backup)
    s3_hot_prefix: str | None
    s3_cold_prefix: str | None

def resolve_user_storage(user_id: str, tier: str) -> StorageLocation:
    if tier == "ec2":
        return StorageLocation(
            tier="ec2",
            s3_bucket=settings.s3_bucket,
            s3_hot_prefix=f"users/{user_id}/hot/",
            s3_cold_prefix=f"users/{user_id}/cold/",
            volume_name=None,
        )
    else:  # hosted
        return StorageLocation(
            tier="hosted",
            volume_name=f"matrx-user-{user_id}",
            s3_bucket=settings.s3_backup_bucket or None,
            s3_hot_prefix=f"users/{user_id}/hot/" if settings.s3_backup_bucket else None,
            s3_cold_prefix=None,
        )
```

The `create_sandbox` flow consults this and either passes S3 env vars (EC2) or creates+mounts a Docker volume (hosted) before `docker run`.

### 4.4 Session manifest (`/home/agent/.matrx/session.json`)

Single source of truth for "what was this sandbox doing." Written by the in-container `matrx_agent` daemon every 60 s and on shutdown.

```jsonc
{
  "schema_version": 1,
  "user_id": "uuid",
  "tier": "ec2",
  "sandbox_id": "sbx-abc",                         // CURRENT sandbox; not what we resume from
  "captured_at": "2026-04-26T18:30:00Z",
  "graceful": true,                                 // false if last write was a periodic checkpoint
  "shell": {
    "cwd": "/home/agent/myproject",
    "history_tail": [ "...last 200 commands..." ]   // truncated, no secrets via filter
  },
  "repos": [
    {
      "path": "/home/agent/myproject",
      "remote_url": "https://github.com/me/myproject.git",
      "default_remote": "origin",
      "branch": "feature-x",
      "head_sha": "abc1234",
      "ahead": 2, "behind": 0,
      "clean": false,
      "uncommitted_files": [
        { "path": "src/foo.ts", "status": "M" },
        { "path": "src/new.ts", "status": "??" }
      ],
      "auto_stash": {
        "stashed_at": "2026-04-26T18:30:00Z",
        "stash_ref": "stash@{0}",
        "stash_message": "matrx-auto-stash 2026-04-26",
        "pushed_branch": "matrx/auto-stash/2026-04-26-1830",
        "pushed_to_remote": true,
        "remote_ref": "refs/heads/matrx/auto-stash/2026-04-26-1830"
      }
    }
  ],
  "processes_at_shutdown": [
    { "pid": 8123, "command": "node dev-server.js", "cwd": "/home/agent/myproject", "rss_kb": 524288 }
  ],
  "ports_at_shutdown": [ 3000 ],
  "transient_things_we_could_not_save": [
    "running processes (Node dev server on :3000)",
    "shell environment variables set with `export`",
    "files outside /home/agent/ (e.g. /tmp/, /etc/)",
    "unstaged ephemeral files in /tmp/",
    "open editor tabs"     // editor state lives in matrx-admin Redux, not in sandbox
  ],
  "size": {
    "home_bytes": 1287000000,
    "biggest_dirs": [
      { "path": "/home/agent/myproject/node_modules", "size_bytes": 850000000 },
      { "path": "/home/agent/.cache", "size_bytes": 320000000 }
    ]
  }
}
```

### 4.5 Auto-stash for uncommitted git work

At shutdown (and at periodic checkpoints), for each git repo under `/home/agent/`:

1. Check if working tree is dirty (`git status --porcelain` non-empty).
2. If yes:
   - `git add -A`
   - `git stash push --include-untracked --message "matrx-auto-stash {ts}"` (preserves locally — the `.git/refs/stash` lives in the synced `.git/` dir).
   - **If the user's git credentials allow** push the stash as a branch:
     - `git branch matrx/auto-stash/{ts} stash@{0}^{tree}` (creates a real commit on top of HEAD)
     - `git push origin matrx/auto-stash/{ts}` (only if `git remote get-url origin` works and credentials are configured)
   - Record both refs in the manifest.
3. **Never silently fail.** If stash fails (lock contention, fatal git error), record that fact in `transient_things_we_could_not_save` so the user sees it.

On resume, the next session's startup hook reads `session.json`, finds auto-stashes, and **shows the user a dialog**: "We saved unfinished work in 2 repos. Apply now / view diff / discard." We never auto-apply — the user makes that call.

### 4.6 Periodic checkpointing

The in-container `matrx_agent` daemon runs a background task: every N minutes (default 5), it:

1. Acquires a per-user lock (`/home/agent/.matrx/locks/checkpoint`) — prevents overlap with shutdown sync.
2. Writes `session.json` (without auto-stash — that's shutdown-only).
3. EC2 tier: incremental `aws s3 sync /home/agent/ s3://.../hot/ --delete --exclude '.matrx/locks/*'`.
4. Hosted tier: nothing extra — Docker volume is already on disk. (If S3 backup is enabled, run a backgrounded `aws s3 sync` here too.)

This bounds data loss on hard kill to ~N minutes. Default is 5; configurable per-user.

### 4.7 The session report

When a new sandbox starts and finds prior data, it writes `/home/agent/.matrx/session-report.md`:

```markdown
# Welcome back

Restored from your last session (closed 2026-04-26 18:30, gracefully).

## ✅ Restored
- 1.3 GB of files in /home/agent/
- 2 git repositories: myproject, scratch
  - myproject — auto-stash available: 4 modified files, 1 new file
    Apply with: `git stash pop` (or click in the editor's Source Control panel)
- Shell history (last 200 commands)

## ❌ Not restored
- 1 process that was running: `node dev-server.js` (port 3000) — restart manually if needed
- Environment variables you set with `export` are gone
- Anything you wrote outside /home/agent/ (root filesystem, /tmp, /etc) is gone

## 💡 You may want to
- Run `git status` in /home/agent/myproject to see your in-flight work
- Restart your dev server if you need it
```

The editor surfaces this on connect — first thing the user sees.

### 4.8 What about cross-tier data portability?

**Phase 1 decision: separate namespaces.** EC2 sandboxes restore from S3; hosted sandboxes restore from their per-user volume. A user who has both types has two independent home directories.

**Future v2:** make S3 authoritative for both. Hosted-tier orchestrator does sync-down from S3 at startup (just like EC2) and sync-up to S3 on shutdown, treating the Docker volume as a fast local cache. This is cleanest but requires AWS creds on the dev server. Defer until we hit a real user pain point.

### 4.9 Quotas + visibility

| Knob | Default | Where enforced |
|---|---|---|
| Per-user hot storage cap | 10 GB | `aws s3 sync` blocks if exceeded; orchestrator surfaces in `/system` and the admin panel |
| Per-user total (hot+cold) | 50 GB | same |
| Per-sandbox concurrent home size | 20 GB (Docker) / 50 GB (FUSE cold) | already enforced by container disk quota |
| Manifest size | 1 MB | truncate `shell.history_tail`, `processes_at_shutdown` to keep within limit |

Surface in two places:
- **Admin panel** ([sandbox-infra page](../../matrx-frontend/app/(authenticated)/(admin-auth)/administration/sandbox-infra/page.tsx)): aggregated usage by tier (already partially done — extend with per-user breakdown).
- **End-user editor**: a "Storage" indicator in the workspace status bar showing `1.3 / 10 GB used` for the current user.

---

## 5. Architecture by tier

### 5.1 EC2 tier — extend, don't replace

Current S3 sync model is solid. Add:

- Manifest writer in the matrx_agent daemon (writes `.matrx/session.json` periodically).
- Auto-stash on shutdown (new bash helper `git-auto-stash.sh` invoked from `shutdown.sh`).
- Periodic checkpointing — currently sync is only on start/end; add a 5-minute cron driven by the daemon.
- SIGKILL-safety: separate small daemon (`session-checkpoint.service`) that writes the manifest every minute even if the main process is wedged.
- Quota enforcement in `hot-sync.sh up` — check size before `aws s3 sync`, log + fail clearly when over.

### 5.2 Hosted tier — build the missing layer

Today's `create_sandbox` for hosted does a bare `docker run`. New flow:

```python
# orchestrator/orchestrator/sandbox_manager.py — hosted branch of create_sandbox
location = resolve_user_storage(user_id, tier="hosted")

# Ensure the user volume exists (idempotent — Docker no-ops if it already does)
client.volumes.create(
    name=location.volume_name,
    driver="local",
    labels={"matrx.user_id": user_id, "matrx.kind": "user-home"},
)

container = client.containers.run(
    ...,
    volumes={location.volume_name: {"bind": "/home/agent", "mode": "rw"}},
    ...
)
```

The Docker volume `matrx-user-{uid}` lives on the host filesystem (under `/var/lib/docker/volumes/`). It survives container destruction; it lives until explicitly deleted via the API or admin panel.

**For the existing starter pool (sandbox-1..5):** mark them deprecated. New flow doesn't use them. Eventually retire them in favor of dynamically-provisioned hosted sandboxes with proper user volumes. Until retirement they still work — they just don't share data with the new dynamic flow.

### 5.3 Cross-cutting changes

Both tiers need the same in-container behavior, so changes go in `sandbox-image/`:

- `sandbox-image/sdk/matrx_agent/persistence/` — new module
  - `manifest.py` — write/read `session.json`
  - `git_autostash.py` — stash all dirty repos, optionally push branches
  - `checkpoint.py` — periodic 5-min job, lock-file managed
  - `session_report.py` — render markdown report on first run
- `sandbox-image/scripts/`
  - `shutdown.sh` — call into the daemon's `/internal/shutdown` route which invokes the persistence module before signalling sync
  - `entrypoint.sh` / `entrypoint-local.sh` — call into the daemon's `/internal/startup` route to write the report on first connect

The daemon already runs at `:8000` with FastAPI; adding internal routes is a few lines. Reusing the daemon means the orchestrator can also query persistence state via the same proxy mechanism we already use for fs/git/etc.

---

## 6. Implementation phases

Five phases. Each is independently shippable + reversible.

### Phase 1 — Hosted-tier persistence parity (the urgent gap) — ~1 day

Goal: hosted sandboxes survive `docker rm` with the user's home dir intact.

- [ ] Add `resolve_user_storage()` helper to orchestrator (`storage_layout.py`).
- [ ] In `create_sandbox`: when `tier == "hosted"`, create+mount per-user Docker volume.
- [ ] In `destroy_sandbox`: do NOT delete the user volume — only the container.
- [ ] Add `volume_name` to `SandboxResponse` so the frontend can surface "your data: 1.3 GB."
- [ ] Volume cleanup endpoint (`DELETE /users/{uid}/volume?force=true`) for explicit reset.
- [ ] **Tests:** create a hosted sandbox, write to /home/agent, destroy, create another → file is there.

**Risk:** none on EC2 (no changes). ~~Hosted tier currently broken in this regard~~ **SHIPPED — hosted per-user volumes (`matrx-user-<uid>`) are live and survive container destroy.**

### Phase 2 — In-container persistence module — ~2 days

Goal: every sandbox writes a session manifest. Crash-safe via periodic checkpoints.

- [ ] New Python package `matrx_agent.persistence` in [sandbox-image/sdk/](../sandbox-image/sdk/).
- [ ] Background asyncio task in the daemon that writes `/home/agent/.matrx/session.json` every 5 min.
- [ ] Internal route `POST /internal/shutdown` that does final manifest + auto-stash + flushes.
- [ ] `entrypoint.sh` / `entrypoint-local.sh` call `POST /internal/startup` after daemon ready — generates `session-report.md` if a previous manifest exists.
- [ ] Add `auto_stash` config to env vars (defaults to enabled).
- [ ] **Tests:** kill -9 the daemon mid-checkpoint; restart; manifest is still readable.

**Risk:** low. New code paths; existing flows unaffected unless `enable_persistence_v2` flag is set.

### Phase 3 — Git auto-stash — ~1 day

Goal: no uncommitted work disappears on shutdown.

- [ ] `git_autostash.py` walks `/home/agent/` for `.git` dirs (max depth 6), `git status --porcelain`, stashes, commits stash to a branch, pushes if `git ls-remote` succeeds.
- [ ] Records every action in `session.json` repos[].auto_stash.
- [ ] Configurable: per-repo opt-out via `.matrx/no-auto-stash` marker file.
- [ ] On startup: surface auto-stashes in `session-report.md`. Editor's Source Control panel ([SandboxGitAdapter](../../matrx-frontend/features/code/adapters/SandboxGitAdapter.ts)) reads from manifest and offers "Apply auto-stash."
- [ ] **Tests:** dirty repo → shutdown → restart → `git stash list` shows the auto-stash; `git log matrx/auto-stash/*` shows the branch.

**Risk:** medium — git operations on weird repo states (detached HEAD, merge in progress, submodules, LFS) need careful handling. Wrap in `try/except` and degrade gracefully.

### Phase 4 — Frontend UX — ~2 days

Goal: the user sees their persistent data + session continuity.

- [ ] **Create-sandbox dialog:** if user has prior data on this tier, show "1.3 GB from your last session — restore?" toggle. Default ON.
- [ ] **Connect flow:** on first message after connect, fetch + render `/home/agent/.matrx/session-report.md` via the existing fs proxy. Show as a non-blocking welcome panel.
- [ ] **Source Control panel:** if `session.json` lists auto-stashes, surface them with "Apply" / "Show diff" / "Discard" actions. Wire to `SandboxGitAdapter.stash({action:'pop'})`.
- [ ] **Storage indicator:** workspace status bar shows `1.3 / 10 GB`. Click → modal with breakdown of biggest dirs.
- [ ] **Settings → Sandbox Data:** list all user data, "Delete persistent storage" button (with double confirmation).
- [ ] **Admin sandbox-infra panel:** add per-user usage rollup table.

**Risk:** UI work in matrx-admin can stall on review. Ship behind a feature flag on the redux side; flip when ready.

### Phase 5 — Quotas, monitoring, and v2 portability — ~1–2 days

Goal: everything bounded, observable, and upgradeable.

- [ ] Quota check in `hot-sync.sh up`: `du -sb` on `/home/agent/`, fail clearly if over the configured cap.
- [ ] `/system` endpoint reports per-user usage (already partially done — extend).
- [ ] Hourly cron on EC2 that checks for orphaned S3 prefixes (no corresponding user) and logs (don't auto-delete).
- [ ] Hourly cron on hosted that checks for orphaned Docker volumes (no corresponding user) and logs.
- [ ] **(Optional v2):** make hosted tier sync to S3 too, with the Docker volume as a local cache. Unifies cross-tier portability. Adds AWS dep on the dev server — only worth it if users complain.

**Risk:** low. All defensive monitoring + opt-in v2.

---

## 7. Schema changes

### 7.1 `sandbox_instances` (Supabase Postgres)

Add columns (nullable, no breaking change):

```sql
ALTER TABLE sandbox_instances
  ADD COLUMN IF NOT EXISTS persistence_volume TEXT,           -- e.g. "matrx-user-<uuid>"
  ADD COLUMN IF NOT EXISTS persistence_size_bytes BIGINT,     -- last-known size
  ADD COLUMN IF NOT EXISTS last_manifest_at TIMESTAMPTZ,      -- last time .matrx/session.json was written
  ADD COLUMN IF NOT EXISTS restored_from_session_id UUID;     -- nullable; points at the manifest used to restore (audit trail)
```

(Goes in `migrations/003_persistence_columns.sql`.)

### 7.2 New `user_persistence` table

Tracks per-user storage independently of any specific sandbox:

```sql
CREATE TABLE IF NOT EXISTS user_persistence (
    user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    -- EC2 tier
    s3_hot_bytes BIGINT DEFAULT 0,
    s3_cold_bytes BIGINT DEFAULT 0,
    -- Hosted tier
    volume_name TEXT,
    volume_bytes BIGINT DEFAULT 0,
    -- Most recent activity
    last_activity_at TIMESTAMPTZ,
    last_manifest_at TIMESTAMPTZ,
    -- Caps (overridable per user; default from settings)
    hot_quota_bytes BIGINT DEFAULT 10737418240,    -- 10 GB
    total_quota_bytes BIGINT DEFAULT 53687091200,  -- 50 GB
    -- Bookkeeping
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE user_persistence ENABLE ROW LEVEL SECURITY;
CREATE POLICY user_persistence_self_only ON user_persistence
    FOR ALL USING (auth.uid() = user_id);
```

Refreshed by the orchestrator's quota-check job (runs after each sync). Surfaced via a new endpoint:

```
GET /users/{user_id}/persistence  (auth required)
  → { hot_bytes, cold_bytes, volume_bytes, hot_quota, total_quota, last_activity_at, last_manifest_at }
```

---

## 8. Critical files

### Sandbox repo

- [orchestrator/orchestrator/sandbox_manager.py](../orchestrator/orchestrator/sandbox_manager.py) — extend `create_sandbox` + `destroy_sandbox` for tier-aware volumes
- [orchestrator/orchestrator/storage_layout.py](../orchestrator/orchestrator/storage_layout.py) — **NEW** — `resolve_user_storage()`
- [orchestrator/orchestrator/routes/users.py](../orchestrator/orchestrator/routes/users.py) — **NEW** — `GET /users/{uid}/persistence`, `DELETE /users/{uid}/volume`
- [orchestrator/migrations/003_persistence_columns.sql](../orchestrator/migrations/003_persistence_columns.sql) — **NEW**
- [orchestrator/migrations/004_user_persistence_table.sql](../orchestrator/migrations/004_user_persistence_table.sql) — **NEW**
- [sandbox-image/sdk/matrx_agent/persistence/](../sandbox-image/sdk/matrx_agent/persistence/) — **NEW package** — manifest, autostash, checkpoint, report
- [sandbox-image/scripts/shutdown.sh](../sandbox-image/scripts/shutdown.sh) — call daemon's `/internal/shutdown` first
- [sandbox-image/scripts/entrypoint.sh](../sandbox-image/scripts/entrypoint.sh) and [entrypoint-local.sh](../sandbox-local/scripts/entrypoint-local.sh) — call daemon's `/internal/startup`
- [sandbox-image/scripts/git-auto-stash.sh](../sandbox-image/scripts/git-auto-stash.sh) — **NEW** — sidecar fallback if daemon is wedged
- [docs/OPERATIONS.md](OPERATIONS.md) — add a "Persistence" section explaining the contract + recovery if a user's data is corrupted/lost

### Frontend repo

- [features/code/views/sandboxes/SandboxesPanel.tsx](../../matrx-frontend/features/code/views/sandboxes/SandboxesPanel.tsx) — add "restore previous session" toggle on create
- [features/code/views/source-control/](../../matrx-frontend/features/code/views/source-control/) — add auto-stash banner + actions (extends the work already pending in PR follow-up)
- [features/code/CodeWorkspace.tsx](../../matrx-frontend/features/code/CodeWorkspace.tsx) — render session-report.md on connect
- [features/code/redux/codeWorkspaceSlice.ts](../../matrx-frontend/features/code/redux/codeWorkspaceSlice.ts) — add `lastSessionMetadata` slot
- [hooks/sandbox/use-sandbox.ts](../../matrx-frontend/hooks/sandbox/use-sandbox.ts) — `getUserPersistence(userId)`, `deleteUserPersistence(userId)`
- [app/api/sandbox/users/[id]/persistence/route.ts](../../matrx-frontend/app/api/sandbox/users/) — **NEW** proxy
- [app/(authenticated)/(admin-auth)/administration/sandbox-infra/page.tsx](../../matrx-frontend/app/(authenticated)/(admin-auth)/administration/sandbox-infra/page.tsx) — add per-user usage table
- [app/(authenticated)/settings/sandbox-data/page.tsx](../../matrx-frontend/app/(authenticated)/settings/sandbox-data/) — **NEW user-facing settings page**

---

## 9. Reuse (don't reinvent)

- **EC2 hot-sync model is solid** — extend, don't replace.
- **The matrx_agent daemon** already runs in every container and exposes HTTP routes — adding `/internal/startup` and `/internal/shutdown` slots in cleanly.
- **`SandboxGitAdapter`** ([adapters/SandboxGitAdapter.ts](../../matrx-frontend/features/code/adapters/SandboxGitAdapter.ts)) already has stash methods. The auto-stash UI builds on `stash({action:'pop'/'list'})`.
- **`SandboxFilesystemAdapter`** can read `/home/agent/.matrx/` like any other path — no new endpoint needed for the report.
- The **orchestrator's proxy pattern** (`@router.api_route("/{sandbox_id}/fs/{path:path}", ...)`) means we don't need new orchestrator endpoints for persistence — the daemon owns those.
- **Supabase RLS** is already wired on `sandbox_instances` — same pattern works for `user_persistence`.
- The **admin panel** (just shipped in PR #4) is the natural home for fleet-wide usage / per-user breakdown.

---

## 10. Verification plan

After Phase 1 (hosted parity):
- Create hosted sandbox → echo "hello" > ~/test.txt → destroy → create another → cat ~/test.txt = "hello".
- Same test on EC2 — confirm nothing regressed.

After Phase 2 (manifest):
- Confirm `/home/agent/.matrx/session.json` is written within 5 min of sandbox start.
- `kill -9` the daemon; restart; manifest still parseable.
- Confirm `transient_things_we_could_not_save` lists running processes by name.

After Phase 3 (auto-stash):
- Create sandbox → clone repo → modify file → don't commit → graceful destroy → create new sandbox.
- `git stash list` shows the auto-stash with our message.
- If credentials configured: `git ls-remote` shows the `matrx/auto-stash/{ts}` branch on origin.
- If credentials missing: `transient_things_we_could_not_save` documents that the stash exists locally only.

After Phase 4 (UX):
- Open `/code` in fresh browser → workspace loads → `session-report.md` panel renders with the last session's summary.
- Source Control panel shows pending auto-stashes with "Apply" buttons that work.
- Settings → Sandbox Data shows usage; "Delete persistent storage" removes everything (with confirm).

After Phase 5 (quotas):
- Fill /home/agent to 10 GB → next sync fails clearly with "quota exceeded."
- Admin panel shows the user red.
- Orphan detector logs orphans without auto-deleting.

---

## 11. Decisions (locked in 2026-04-26)

1. **AWS creds on the dev server** — **Yes, provision them** (Phase 6). User says "I think we should just always sync it" for cross-tier portability. Until creds land, the hosted tier persists locally only via Docker volume.
2. **Quota defaults** — 10 GB hot / 50 GB total **per user**. Per-user override stored on `user_persistence.hot_quota_bytes` / `total_quota_bytes` so admins can lift caps for power users. Phase 5 enforces.
3. **Auto-stash branch policy** — **Push to remote always when creds work, never when they don't.** Result captured in `session.json`'s `auto_stash` field so users always know which case applied. (✅ shipped today)
4. **Cold storage** — Always sync to S3 on **both tiers**. Same as hot. Hosted-tier S3 sync is part of Phase 6 and depends on AWS creds.
5. **Org/team data sharing** — Defer. v1 is per-user only. Refactor `user_persistence` → `org_persistence` if/when org sharing ships.
6. **Starter pool (`sandbox-1`…`sandbox-5`)** — **Retire.** They were never intended for real users. Replaced by the dynamic per-user volume flow shipped today. Documented in `/srv/projects/matrx-sandbox/CLAUDE.md`.

## 12. Audit findings — existing infrastructure we can leverage (not built today, future Phase 6+)

A thorough audit on 2026-04-26 found **production-grade file-persistence infrastructure already in the org**, none of which is currently wired to the sandbox. These are deferred integration points, not blockers:

### `matrx-utils.cloud_sync` (in aidream)

Path: `/srv/projects/aidream/packages/matrx-utils/matrx_utils/file_handling/cloud_sync/`. S3 / server-filesystem sync engine with **versioning, permissions, share links, group ACLs**, async + sync APIs, and a complete Postgres schema (`cld_files`, `cld_folders`, `cld_file_versions`, `cld_file_permissions`, `cld_share_links`, `cld_user_groups`). Used by aidream production.

**Sandbox integration opportunity:** instead of the orchestrator's bespoke `hot-sync.sh` + `cold-mount.sh`, the in-container `matrx_agent.persistence` module could call `cloud_sync.managed_write_async` for individual files at checkpoint time — gaining versioning, audit trail, share links for free. **Phase 6.**

### `code_files` + `code_file_folders` (in matrx-frontend)

Path: `/srv/projects/matrx-frontend/features/code-files/`. The "Code Library" — user-facing file tree with S3 offload at 50 KB threshold, auto-save middleware (1.5–5s adaptive debounce), Postgres-resident metadata + S3 content. Bridges to the editor via `useSaveAndOpenInCodeEditor()`.

**Sandbox integration opportunity:** add a "Save to Library" action in the sandbox file browser that writes selected files into `code_files`. Surface code_files as a virtual directory inside the sandbox via FUSE so users see "their library" alongside the per-user volume. **Phase 6.**

### Abandoned: `user_files` table

Predecessor to `cld_files`. Has a Postgres table but zero code references in features. Safe to leave; do not extend.

---

## 13. Phase status legend

- **Phase 1** ✅ shipped + verified
- **Phase 2** ✅ shipped + verified
- **Phase 3** ✅ shipped + verified
- **Phase 4** (frontend UX) — ✅ shipped by the matrx-frontend team (auto-stash UI in source-control, `useUserPersistence`, session-report opener, `/api/sandbox/persistence` proxy)
- **Phase 5** (quotas + monitoring) — pending; backend ready (`get_user_volume_size`)
- **Phase 6a** (AI Dream cloud_files bridge) — sandbox-side **shipped** (`mtx` CLI, `cloud-files-sync.sh`, env-var passthrough); waiting on AI Dream side. **Spec: [AIDREAM_INTEGRATION.md](AIDREAM_INTEGRATION.md).**
- **Phase 6b** (cross-tier S3 portability for hosted tier) — depends on AWS creds in `/srv/apps/sandbox-orchestrator/.env`

---

## 12. What this plan does NOT solve

For honesty:

- **Process resume.** A user running `npm run dev` on port 3000, then closing their laptop, can't expect that process to be running when they come back. We document this but don't try to fix it. (A future "background exec + auto-restart" feature could layer on top using §3.2.4 of the wishlist.)
- **Editor state (open tabs, cursor, panel sizes).** Lives entirely in matrx-admin Redux/local storage — orthogonal to sandbox persistence. The editor team owns that.
- **Cross-machine portability of the editor.** If you connect to a sandbox from a new browser, your editor state is fresh — but your sandbox's `/home/agent/` is the same. That's correct.
- **Branch protection in user repos.** If we auto-push `matrx/auto-stash/*` branches and the user's repo has branch protection that disallows it, the push fails. We log and continue — same as today's "credentials missing" path.
- **Long-term retention / archival.** This plan keeps user data forever. We'll need a retention policy (delete data for users idle 90d? quote into archive tier?) but it's separate.

---

## 13. Summary for the impatient

| Question | Answer |
|---|---|
| Is user data persisted today? | EC2: yes (S3, working). Hosted: no (data lost on container destroy). |
| Where? | EC2: `s3://matrx-sandbox-storage-prod-2024/users/{uid}/hot|cold/`. Hosted: nowhere (urgent). |
| What's the contract going to be? | "Anything in `/home/agent/` survives, including `.git/`. We tell you what we couldn't save." |
| How do we capture "the environment"? | A `session.json` manifest written every 5 min + on shutdown. Lists repos, processes, cwd, ports. |
| What about uncommitted git changes? | Auto-stashed locally, pushed to a `matrx/auto-stash/{ts}` branch when creds allow. |
| What's NOT preserved? | Running processes, env vars set in the shell, anything outside `/home/agent/`. We surface this in a session report. |
| Can we make the tiers behave the same? | Yes — Phase 1 adds per-user Docker volumes on hosted to match EC2's per-user S3. |
| When? | Phase 1 (hosted parity): ~1 day. Full plan: ~5–7 days, phased. |
| What breaks today? | Nothing — every phase is additive and shippable independently. |
