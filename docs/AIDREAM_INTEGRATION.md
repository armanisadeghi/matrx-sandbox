# AI Dream ↔ Sandbox Integration

**Status:** Sandbox-side plumbing shipped 2026-04-26 (orchestrator env-var passthrough, in-container `mtx` CLI, `cloud-files-sync.sh` bridge). AI Dream side: **needs implementation** — five REST endpoints + a service-token verification step.

This doc is the contract. AI Dream owns its end of the wire. Sandbox-side is built and waiting.

---

## The user-facing pitch

> A user uploads a 200-page legal brief in the AI Dream UI. They open a sandbox. The agent sees `/home/agent/cloud-files/case-2026-Q1/brief.pdf` and runs `pdftotext brief.pdf - | grep "limitation period"` natively. No special API calls, no intermediate uploads — just the agent's normal shell tools over a real filesystem.

That's the full story. Everything below is plumbing for that one user need.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ matrx-frontend (matrx-admin)                                         │
│  user uploads → /code-files API → cld_files (Supabase RLS-scoped)    │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
                  ┌──────────▼────────────┐
                  │  AI Dream backend     │      Two roles:
                  │  (FastAPI)            │       • Owns cld_files schema
                  │                       │       • Exposes a thin REST
                  │   /api/cloud-files/*  │         "bridge" surface for
                  │                       │         sandboxes (this doc)
                  └──────────┬────────────┘
                             │
                  ┌──────────▼────────────┐
                  │  Sandbox Orchestrator │      Holds one shared
                  │                       │      service token + injects
                  │  Injects env vars     │      into every spawned sbx
                  │  per spawned sandbox  │
                  └──────────┬────────────┘
                             │
                  ┌──────────▼─────────────────────┐
                  │  Sandbox container             │
                  │                                │
                  │  /home/agent/cloud-files/  ←──┐│   On startup:
                  │  (synced from cld_files)     ││   cloud-files-sync.sh down
                  │                              ││
                  │  $ mtx files ls              ││   On shutdown:
                  │  $ mtx files cat brief.pdf   ││   cloud-files-sync.sh up
                  │  $ mtx files put report.md   ││
                  │  $ git/grep/sed/cat anywhere ─┘
                  └────────────────────────────────┘
```

---

## Authentication model — service token + user header

**Goal:** sandboxes can act on behalf of a specific user without holding the user's actual credential.

**How it works:**

1. **One shared service token** lives in the orchestrator's env (`MATRX_AIDREAM_SERVICE_TOKEN`). The orchestrator injects it into every spawned sandbox as `MATRX_AIDREAM_SERVICE_TOKEN`. AI Dream knows this same value (`AIDREAM_SANDBOX_SERVICE_TOKEN` on its side).
2. The sandbox calls AI Dream:
   ```
   GET https://api.aidream.ai/api/cloud-files/list
     Authorization: Bearer <service_token>
     X-Matrx-User-Id: <user_uuid>      ← orchestrator-injected USER_ID env var
   ```
3. AI Dream verifies:
   - `Authorization` token matches `AIDREAM_SANDBOX_SERVICE_TOKEN` (constant-time compare).
   - `X-Matrx-User-Id` is a valid UUID and corresponds to a real user.
4. AI Dream queries `cld_files` scoped to `WHERE owner_id = <X-Matrx-User-Id>`. RLS bypassed because we're using a service-role connection — but we re-impose ownership in the WHERE clause.

**Why not a per-user JWT?**

- Sandboxes are long-lived; JWTs would expire mid-session.
- Service token + user-id header gives the orchestrator the choice of *which* user the sandbox represents — useful for impersonation flows we may want later (admin creates a sandbox on behalf of a user).
- Service token never leaves the orchestrator process or its spawned containers. Users never see it. A leaked container API key shouldn't expose user data on AI Dream because the bridge endpoints scope every read by `X-Matrx-User-Id` — and that header is set by the orchestrator at create time.

**Auth failure modes:**

| Scenario | AI Dream returns |
|---|---|
| Missing or wrong service token | 401 |
| Missing `X-Matrx-User-Id` | 400 |
| Header user_id is not a UUID | 400 |
| Header user_id doesn't exist in `auth.users` | 404 (don't leak existence) |
| User exists but is disabled | 403 |

---

## Endpoints AI Dream needs to expose

All under `/api/cloud-files/*`. All require the service token + user-id header described above.

### `GET /api/cloud-files/list`

List the files for the user. Backend: `SELECT id, file_path, file_size, mime_type, current_version, updated_at FROM cld_files WHERE owner_id = $1 AND deleted_at IS NULL ORDER BY file_path`.

**Query params:**
- `prefix` (optional) — filter to a path prefix (e.g. `prefix=case-2026-Q1/`).
- `limit` (default 1000, max 5000) — pagination cap.
- `cursor` (opaque) — pagination token.

**Response:**
```jsonc
{
  "files": [
    {
      "id": "<uuid>",
      "file_path": "case-2026-Q1/brief.pdf",
      "file_size": 4521824,
      "mime_type": "application/pdf",
      "current_version": 3,
      "updated_at": "2026-04-26T18:30:00Z"
    },
    ...
  ],
  "next_cursor": null
}
```

### `GET /api/cloud-files/get?path=<path>`

Stream a single file's bytes. Backend: query `cld_files`, fetch the latest version's `storage_uri`, stream from cloud_sync's S3/Supabase backend.

**Response:** binary content; `Content-Type` from `cld_files.mime_type`; `Content-Length` from `file_size`. 404 if not found, 403 on RLS-equivalent failures.

### `PUT /api/cloud-files/put`

Upload a file. Multipart form:
- `file` — the file content (one or more chunks)
- `file_path` — destination path (relative to user's root)

If a file already exists at that path, write a new version (cld_files versioning is automatic via SyncEngine).

**Response:** `{ id, file_path, version, size_bytes }`. 413 on quota exceeded.

### `DELETE /api/cloud-files/delete?path=<path>`

Soft-delete (set `cld_files.deleted_at`). Returns 204.

### `GET /api/cloud-files/quota`

Report the user's storage usage and limit. Drives the editor's "1.3 / 10 GB" indicator.

**Response:**
```jsonc
{
  "used_bytes": 1287000000,
  "quota_bytes": 10737418240,
  "files_count": 84
}
```

---

## Implementation hint for AI Dream — wire-up

The cloud_sync module already does all the hard work. The bridge endpoints are mostly thin:

```python
# aidream/api/cloud_files_bridge.py  (sketch)
from fastapi import APIRouter, Depends, HTTPException, Header, UploadFile
from matrx_utils import FileManager
from matrx_utils.file_handling.cloud_sync import CloudSyncConfig

router = APIRouter(prefix="/api/cloud-files", tags=["sandbox-bridge"])

def verify_service_token(authorization: str = Header(...)) -> None:
    expected = settings.AIDREAM_SANDBOX_SERVICE_TOKEN
    if not authorization.startswith("Bearer "):
        raise HTTPException(401)
    presented = authorization[7:]
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(401)

def verify_user_header(x_matrx_user_id: str = Header(...)) -> str:
    try:
        UUID(x_matrx_user_id)
    except ValueError:
        raise HTTPException(400, "X-Matrx-User-Id must be a UUID")
    # Optionally check user exists / is active here.
    return x_matrx_user_id

def fm_for_user(user_id: str = Depends(verify_user_header),
                _: None = Depends(verify_service_token)) -> FileManager:
    fm = FileManager("aidream-sandbox-bridge", cloud_sync=CloudSyncConfig(auto_sync=True))
    fm.sync_engine.set_user(user_id)   # scopes all reads/writes to this user
    return fm


@router.get("/list")
async def list_files(prefix: str = "", fm: FileManager = Depends(fm_for_user)):
    files = await fm.sync_engine.list_files_async(prefix=prefix)
    return {"files": files, "next_cursor": None}


@router.get("/get")
async def get_file(path: str, fm: FileManager = Depends(fm_for_user)):
    data = await fm.sync_engine.managed_read_async(path)
    return Response(content=data, media_type=guess_type(path))


@router.put("/put")
async def put_file(file: UploadFile, file_path: str = Form(...),
                   fm: FileManager = Depends(fm_for_user)):
    content = await file.read()
    result = await fm.sync_engine.managed_write_async(
        file_path=file_path, content=content, mime_type=file.content_type,
    )
    return {"id": result.file_id, "file_path": file_path,
            "version": result.version, "size_bytes": len(content)}


@router.delete("/delete", status_code=204)
async def delete_file(path: str, fm: FileManager = Depends(fm_for_user)):
    await fm.sync_engine.delete_async(path)


@router.get("/quota")
async def get_quota(fm: FileManager = Depends(fm_for_user)):
    return await fm.sync_engine.get_quota_async()
```

Mount the router with no prefix tweak. The cloud_sync layer already enforces per-user RLS via the path prefix model (`s3://bucket/<owner_id>/...`), so the only thing the bridge has to add is the service-token gate.

---

## What's already shipped on the sandbox side (verified 2026-04-26)

In every sandbox container:

- **`/usr/local/bin/mtx`** — Python CLI shim. Subcommands:
  - `mtx files ls` / `cat <path>` / `put <local> [<remote>]` / `rm <path>`
  - `mtx files sync down --dest <dir>` / `sync up --src <dir>`
  - `mtx whoami` (works without AI Dream — useful for debugging)
- **`/opt/sandbox/scripts/cloud-files-sync.sh down|up`** — wraps `mtx files sync` with a 60 s timeout. No-ops cleanly if AI Dream env vars are absent.
- **Hooks already in place:**
  - `entrypoint.sh` (production EC2) → calls `cloud-files-sync.sh down` after the daemon is up.
  - `entrypoint-local.sh` (hosted tier) → same.
  - `shutdown.sh` / `shutdown-local.sh` → calls `cloud-files-sync.sh up` before container stop.
- **Orchestrator passes:** `MATRX_AIDREAM_URL`, `MATRX_AIDREAM_SERVICE_TOKEN`, `USER_ID`, plus AWS creds (hosted tier).

The whole pipeline already runs end-to-end. It just no-ops gracefully today because `MATRX_AIDREAM_URL` isn't configured. Once the AI Dream side ships the five endpoints above and you set the orchestrator env vars, every new sandbox will automatically pull the user's cloud_files into `~/cloud-files/` at startup and push changes back at shutdown.

---

## Configuration checklist (when AI Dream side ships)

In `/srv/apps/sandbox-orchestrator/.env` on this server:

```
MATRX_AIDREAM_URL=https://api.aidream.example.com
MATRX_AIDREAM_SERVICE_TOKEN=<paste from AI Dream's secrets>
# AWS creds for hosted-tier S3 sync (Phase 6) — separate from AI Dream
MATRX_AWS_ACCESS_KEY_ID=...
MATRX_AWS_SECRET_ACCESS_KEY=...
```

Then `cd /srv/apps/sandbox-orchestrator && docker compose restart`.

On AI Dream's side:

```
AIDREAM_SANDBOX_SERVICE_TOKEN=<same token>
# (cloud_sync's existing AWS / Supabase config stays as-is)
```

The token must match exactly on both sides — that's the only secret that has to round-trip.

---

## Smoke test (after both sides are configured)

Inside any sandbox:

```bash
mtx whoami    # should show aidream.configured: true
mtx files ls  # should list the user's cld_files
echo "hello" > /home/agent/cloud-files/test.txt
mtx files put /home/agent/cloud-files/test.txt
# log out of sandbox, refresh the AI Dream Files panel — file appears.
```

---

## Open design questions for the AI Dream team

1. **Path namespace** — `cld_files.file_path` is the user's chosen path. How do we handle files the user has at the same path but different versions (cld_files supports versioning)? **Suggestion:** the bridge's `/get` returns the latest version; we don't expose version history through the bridge for now. Add `?version=N` later if needed.
2. **Quotas + 413 handling** — when a user is over quota, does AI Dream's `/put` return 413 with how-much-over, or just refuse? Frontend wants to show "you're 200 MB over your 10 GB limit."
3. **Streaming uploads** — for files >100 MB, do we want chunked PUT or just rely on HTTP/multipart? Current `mtx files put` uses multipart and works fine up to ~1 GB on httpx.
4. **Conflict resolution** — what if the user edited the same file via the AI Dream UI WHILE a sandbox was holding a copy? Right now the sandbox's shutdown sync would clobber the UI's change. Probably fine for v1; flag in the session report when we detect a clobber (compare `current_version` at startup vs at shutdown).
5. **Listing 50,000 files efficiently** — for power users with massive case files. We probably want server-side pagination + a manifest checksum so the sandbox can skip the full list when nothing changed.

These can land iteratively — the v1 bridge doesn't need to solve any of them.
