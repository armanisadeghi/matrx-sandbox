# FOUND DEFECTS

> Open defects + their workarounds, surfaced loudly so every agent and operator sees them. Each entry stays open until a human explicitly acknowledges and removes it.

---

## EC2-tier cloud-files bridge unreachable (Bug 7, 2026-06-02)

**Symptoms (in sandbox container logs):**

```
cloud-files: marker absent + bridge unreachable; retrying in 60s (attempt 1)
cloud-files: marker absent + bridge unreachable; retrying in 120s (attempt 2)
cloud-files: marker absent + bridge unreachable; retrying in 300s (attempt 3)
```

The matrx_agent daemon's `cloud_sync.downstream` cannot reach `${MATRX_AIDREAM_URL}/api/cloud-files/*` from inside the EC2-tier sandbox, so cloud-file syncing is permanently disabled there. Sandbox boots fine; only cloud-files integration is broken.

**Root cause:**

The EC2 orchestrator's "aidream passthrough" reads `/srv/projects/aidream/.env` to populate `MATRX_AIDREAM_SERVICE_TOKEN` (and related). That path only exists on the `/srv` (hosted-tier) server, not on the EC2 instance — the EC2 orchestrator gets `configured_count: 0` on its aidream-passthrough probe. So the in-container `cloud-files-sync.sh` sees `MATRX_AIDREAM_SERVICE_TOKEN` unset and skips cleanly, but the `matrx_agent` daemon's polling subscriber doesn't have the same skip-on-missing-token branch, hence the noisy retries.

**Impact:**

- ❌ EC2-tier sandboxes cannot pull / push `cld_files` from / to AI Dream.
- ✅ Sandbox creation, container runtime, `secrets` injection, fs / shell / git tools — all fine.
- ✅ Hosted tier (`/srv`) is NOT affected — it has `/srv/projects/aidream/.env`.

**Workaround:**

Surface `MATRX_AIDREAM_SERVICE_TOKEN` to the EC2 orchestrator via SSM Parameter Store or an explicit env var on the EC2 deployment (in `infra/` Terraform). After the orchestrator restarts with the token populated, its passthrough probe will report `configured: true` and the sandbox env passthrough will deliver `MATRX_AIDREAM_SERVICE_TOKEN` into each container.

**Related fix shipped (not the root cause):**

`cloud-files-sync.sh` was hardened (2026-05-28, commit 858ac34) to resolve `HOME` via `getent passwd` instead of trusting the inherited `HOME=/root` from `sudo -E -u agent`, so the entrypoint no longer logs `mkdir: cannot create directory '/root'`. That was a separate boot-time noise bug.

**Tracking:**

This entry stays open until the EC2 orchestrator's aidream passthrough reports `configured: true` and the in-container daemon stops the "bridge unreachable" retries.

---
