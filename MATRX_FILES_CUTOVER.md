# TASK: route this repo's file traffic to the matrx-files microservice

**Service is LIVE:** `https://files.matrxserver.com` (AWS us-east-1, Supabase-JWT auth, identical
wire contract to aidream's `/files/*`, `/assets/*`, `/share/*`).

## What to do

1. **Find where this repo sends file requests to aidream** — the backend base-URL constant used for
   `/files/...`, `/assets/...`, `/share/...` (byte uploads/downloads, URL minting, asset ops).
2. **Add a SEPARATE files base URL** (e.g. `MATRX_FILES_URL` / a config field) used ONLY for those
   file routes. Never point the whole backend URL at the files service — it does not serve
   chat/agents/anything non-file.
3. **Point the file routes at `https://files.matrxserver.com`** and ship.

## Which routes are ready vs not

- **Ready now** (service serves these): core `/files/*` (get/upload/download/url/restore/bulk/asset,
  patch/delete), `/assets/*`, `/share/*`.
- **Not yet** (still on aidream — leave these pointed at the backend for now): `/images/*`,
  `/utilities/pdf/*`, `/media/*`, `/podcast/*`, `/files/{id}/search`, `/folders/*`, file
  analysis/annotations/regions. The service will grow these; until then they stay on aidream.

## Verify you're cut over

On the aidream dashboard `/logs` → filter feature `file-cutover-shadow` → find your client by its
origin / user-agent. Your **`ready` tier must go silent** once you've cut over. That's the objective
proof. (The guard is observe-only — it never blocks you; it just tells us who's still calling.)

Full contract + region confirmation: `aidream/docs/handoffs/matrx-files-cutover.md` and
`/Users/armanisadeghi/code/common-docs/matrx-files-service/FEATURE.md`.
