"""migrate_sandbox — swap a running box onto the current image, no id change.

Phase 2 of the zero-drift system: the per-box migration primitive. A box's DATA
lives OUTSIDE the container (per-user Docker volume mounted at /home/agent, or
S3), so migrating is:

  1. create a NEW container on the target image, mounting the SAME volume and
     carrying the SAME logical sandbox_id (so the agent's binding base_url, which
     is /sandboxes/<sandbox_id>, stays valid across the swap);
  2. wait for readiness + verify it came up on the expected version;
  3. atomically cut over (stop+rename old, rename new to the real id);
  4. destroy the old container.

On ANY failure before cutover, the new container is removed and the OLD box is
left running untouched — then we alarm loudly. Never silently break a session.

Quiescing the chat (so the agent isn't mid-write during the swap) is the
CALLER's responsibility — aidream parks the turn via its suspend/resume gate
before calling this. This primitive is safe to run directly on an idle box.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from docker.errors import APIError, NotFound

from orchestrator.config import settings
from orchestrator.runtime_isolation import container_runtime_isolation
from orchestrator.versioning import current_image

logger = logging.getLogger(__name__)


def _refresh_platform_environment(existing: list[str]) -> tuple[list[str], int]:
    """Replace orchestrator-owned passthrough values without touching user env.

    Docker container environments are immutable.  When the platform database
    or another shared service moves, already-running sandboxes therefore keep
    the old values until they are recreated.  The passthrough name registry is
    the boundary between platform-owned configuration and per-sandbox/user
    configuration: remove those names from the old environment, then append
    the values currently loaded by the orchestrator.

    Values and key names are deliberately absent from the return metadata so a
    migration response or log cannot disclose secrets.
    """
    from orchestrator.sandbox_manager import _resolve_passthrough_keys

    platform_keys = set(_resolve_passthrough_keys())
    old_values: dict[str, str] = {}
    preserved: list[str] = []
    for item in existing:
        key, separator, value = item.partition("=")
        if separator and key in platform_keys:
            old_values[key] = value
        else:
            preserved.append(item)

    current_values = {
        key: value for key in sorted(platform_keys)
        if (value := os.environ.get(key))
    }
    changed = sum(
        1 for key in platform_keys
        if old_values.get(key) != current_values.get(key)
    )
    preserved.extend(f"{key}={value}" for key, value in current_values.items())
    return preserved, changed


def _binds_to_volumes(host_config: dict) -> dict:
    """Rebuild docker-py's ``volumes=`` dict from a container's HostConfig.Binds."""
    out: dict = {}
    for b in host_config.get("Binds") or []:
        parts = b.split(":")
        if len(parts) >= 2:
            out[parts[0]] = {"bind": parts[1], "mode": parts[2] if len(parts) > 2 else "rw"}
    return out


async def _wait_container_ready(container, timeout: int, template: str | None = None) -> bool:
    elapsed, interval = 0, 2
    while elapsed < timeout:
        try:
            await asyncio.to_thread(container.reload)
            if container.status == "exited":
                return False
            readiness = "test -f /tmp/.sandbox_ready"
            if template == "aidream":
                readiness += (
                    " && curl -fsS http://127.0.0.1:8001/api/health/ready >/dev/null"
                    " && AIDREAM_WORK_DIR=/opt/aidream-template"
                    " AIDREAM_IMAGE_SHA_FILE=/etc/aidream-image-sha"
                    " GIT_CONFIG_GLOBAL=/dev/null"
                    " /bin/bash --noprofile --norc -p"
                    " /opt/sandbox/scripts/aidream-helpers.sh verify-release >/dev/null"
                )
            code, _ = await asyncio.to_thread(container.exec_run, readiness)
            if code == 0:
                return True
        except (NotFound, APIError) as exc:
            logger.warning("migrate: readiness poll error: %s", exc)
            return False
        await asyncio.sleep(interval)
        elapsed += interval
    return False


async def _container_version(container) -> str | None:
    """Read /etc/sandbox-image-version from inside the box (the image-baked file,
    not the overridable env) — the source of truth for what version it came up on."""
    try:
        code, out = await asyncio.to_thread(container.exec_run, "cat /etc/sandbox-image-version")
        if code == 0:
            return (out or b"").decode("utf-8", errors="replace").strip() or None
    except (NotFound, APIError):
        pass
    return None


async def _safe_get(store, sandbox_id: str):
    try:
        return await store.get(sandbox_id)
    except Exception:
        return None


def _has_recent_heartbeat(sbx) -> bool:
    """True if the sandbox row has a heartbeat within the configured window."""
    if sbx is None:
        return False
    hb = getattr(sbx, "last_heartbeat_at", None)
    if hb is None:
        return False
    from datetime import datetime, timezone
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - hb).total_seconds()
    return age < settings.migrate_recent_heartbeat_seconds


async def migrate_sandbox(sandbox_id: str, *, store, target_image: str | None = None,
                          verify_timeout: int = 90, require_idle: bool = False,
                          refresh_platform_env: bool = False) -> dict:
    """Migrate one box to the current image (or an explicit target_image).
    Returns a status dict; never raises.

    ``require_idle`` (used by the rolling auto-migrator): refuse to start if the
    box has any in-flight tool call, so a call already mid-execution is never
    caught by the swap. Defers to the next pass instead."""
    from orchestrator import activity
    from orchestrator.sandbox_manager import _get_docker_client

    # The idle gate runs FIRST — before any docker lookups — so a busy box
    # defers with zero side effects (and the gate is testable without docker).
    if require_idle:
        if activity.inflight_count(sandbox_id) > 0:
            return {"status": "busy_deferred", "sandbox_id": sandbox_id,
                    "reason": "box has in-flight tool calls; defer migration to an idle gap"}
        # An attached interactive session (PTY terminal, fs-watch websocket) is
        # ALWAYS busy — never swap a box out from under an open editor/terminal,
        # even if no command is executing this instant.
        if activity.open_session_count(sandbox_id) > 0:
            return {"status": "busy_deferred", "sandbox_id": sandbox_id,
                    "reason": "box has an open interactive session (PTY/watch); defer until it closes"}
        # Recent tool activity = an agent mid-task between commands. Hosted
        # boxes rarely heartbeat, so orchestrator-side activity is the real
        # "recently in use" signal; reuse the heartbeat window as the cutoff.
        age = activity.last_activity_age(sandbox_id)
        if age is not None and age < settings.migrate_recent_heartbeat_seconds:
            return {"status": "busy_deferred", "sandbox_id": sandbox_id,
                    "reason": f"tool activity {int(age)}s ago (< {settings.migrate_recent_heartbeat_seconds}s); defer until idle"}
        if _has_recent_heartbeat(await _safe_get(store, sandbox_id)):
            return {"status": "busy_deferred", "sandbox_id": sandbox_id,
                    "reason": "box had a recent heartbeat; defer migration until idle"}

    client = _get_docker_client()
    try:
        old = await asyncio.to_thread(client.containers.get, sandbox_id)
    except NotFound:
        return {"status": "not_found", "sandbox_id": sandbox_id}

    labels = old.labels or {}
    template = labels.get("matrx.template")
    cur = await asyncio.to_thread(current_image, client, template)
    target = target_image or cur.tag
    old_image_id = (old.attrs or {}).get("Image")

    cfg = old.attrs.get("Config") or {}
    host = old.attrs.get("HostConfig") or {}
    # Copy the old env EXCEPT MATRX_IMAGE_VERSION — the new image must report its
    # OWN baked version (otherwise the box would advertise the stale version and
    # the very drift we just fixed would look unfixed).
    env = [
        e for e in (cfg.get("Env") or [])
        if not e.startswith("MATRX_IMAGE_VERSION=") and not e.startswith("SANDBOX_MIGRATION=")
    ]
    platform_env_changes = 0
    if refresh_platform_env:
        env, platform_env_changes = _refresh_platform_environment(env)

    if (
        not target_image
        and cur.image_id
        and old_image_id == cur.image_id
        and platform_env_changes == 0
    ):
        return {
            "status": "already_current", "sandbox_id": sandbox_id,
            "version": cur.version, "platform_env_changed": 0,
        }
    # Tell the new container's entrypoint it's a MIGRATION boot, not a fresh
    # spawn: the data is already on the volume we're re-mounting, so it skips the
    # (up to 60s) cloud_files down-sync — the biggest chunk of migration time.
    env.append("SANDBOX_MIGRATION=1")
    volumes = _binds_to_volumes(host)
    tmp_name = f"{sandbox_id}-mig"

    # ── DATA-SAFETY GUARD ─────────────────────────────────────────────────────
    # The proven-safe swap relies on the new container mounting the SAME
    # persistent volume at /home/agent as the old one (hosted tier), so the data
    # is physically shared and never copied. The ec2 tier has NO such volume —
    # the home dir is S3-backed (hot-sync on boot, up-sync on shutdown). A naive
    # build-then-cutover there would let the new box hot-sync STALE S3 before the
    # old box flushes its final state at cutover → silent data loss. Until the
    # S3-sync-ordered path is built + tested (quiesce → up-sync old → boot new
    # from fresh S3 → cutover), REFUSE to migrate a box whose /home/agent isn't a
    # shared volume. This makes auto-migrate structurally safe on every tier.
    if "/home/agent" not in {v.get("bind") for v in volumes.values()}:
        if not settings.enable_s3_migrate:
            logger.info(
                "migrate %s: home dir not on a shared volume (S3-backed/ec2) — refusing "
                "(set MATRX_ENABLE_S3_MIGRATE=1 after validating the sync-ordered path)",
                sandbox_id,
            )
            return {
                "status": "unsupported_storage", "sandbox_id": sandbox_id,
                "reason": ("home dir is not on a shared persistent volume (S3-backed / ec2 tier); "
                           "S3-ordered migration is implemented but disabled — enable "
                           "MATRX_ENABLE_S3_MIGRATE only after validating it end-to-end"),
            }
        return await _migrate_s3_ordered(
            sandbox_id, old=old, target=target, env=env, volumes=volumes,
            labels=labels, host=host, cur=cur, store=store, verify_timeout=verify_timeout,
        )

    # Lock for the WHOLE migration up front. While building the new container,
    # both it and the old one carry the same matrx.sandbox_id label, and the
    # store row / container lookup briefly flux — so rather than let calls race
    # that (they 404'd / timed out in testing), we refuse every call with a
    # retryable 503 from here until release. The agent's tool proxy waits it out
    # and lands on the migrated box. We only auto-migrate idle boxes, so in
    # practice few/no calls arrive during the window.
    from orchestrator import activity

    await activity.mark_migrating(sandbox_id)
    new = None
    try:
        try:
            await asyncio.to_thread(lambda: client.containers.get(tmp_name).remove(force=True))  # clear any stale temp
        except NotFound:
            pass
        except APIError as exc:
            logger.warning("migrate %s: could not clear stale temp container: %s", sandbox_id, exc)

        logger.info(
            "migrate %s: %s -> %s (template=%s)",
            sandbox_id, (old_image_id or "?")[:19], target, template,
        )
        tier = labels.get("matrx.tier")
        run_kwargs: dict = dict(
            image=target, name=tmp_name, detach=True, environment=env,
            volumes=volumes or None, network=settings.docker_network,
            ports={"22/tcp": None}, extra_hosts={"host.docker.internal": "host-gateway"},
            labels=labels, restart_policy={"Name": "no", "MaximumRetryCount": 0},
        )
        run_kwargs.update(container_runtime_isolation(template, tier))
        if host.get("NanoCpus"):
            run_kwargs["nano_cpus"] = host["NanoCpus"]
        if host.get("Memory"):
            run_kwargs["mem_limit"] = host["Memory"]

        try:
            new = await asyncio.to_thread(lambda: client.containers.run(**run_kwargs))
        except APIError as exc:
            logger.error("migrate %s: new container create failed: %s", sandbox_id, exc)
            return {"status": "failed", "sandbox_id": sandbox_id, "reason": f"create failed: {exc}"}

        ready = await _wait_container_ready(new, verify_timeout, template)
        new_ver = (await _container_version(new)) if ready else None
        # Skip strict version equality when the current image is itself
        # unversioned (built before the stamp) — fall back to readiness + image id.
        version_ok = cur.version is None or new_ver == cur.version
        if not ready or not version_ok:
            logger.error(
                "MIGRATE FAILED %s: ready=%s new_version=%s expected=%s — OLD box kept running, ALARM",
                sandbox_id, ready, new_ver, cur.version,
            )
            try:
                await asyncio.to_thread(new.remove, force=True)
            except APIError:
                pass
            return {
                "status": "failed", "sandbox_id": sandbox_id,
                "reason": f"new box not ready / version mismatch (ready={ready}, version={new_ver}->{cur.version})",
            }

        # Drain calls that were already in-flight when we locked, so cutover
        # never interrupts a tool mid-execution. New calls are already refused.
        if not await activity.drain_inflight(sandbox_id, timeout=20.0):
            logger.info("migrate %s: box busy, deferring (will retry later)", sandbox_id)
            try:
                await asyncio.to_thread(new.remove, force=True)
            except APIError:
                pass
            return {"status": "busy_deferred", "sandbox_id": sandbox_id,
                    "reason": "in-flight tool calls did not drain; retry later"}

        # ── Atomic cutover ────────────────────────────────────────────────────
        old_renamed = f"{sandbox_id}-old-{int(time.time())}"
        try:
            try:
                await asyncio.to_thread(old.stop, timeout=settings.shutdown_timeout_seconds)
            except APIError:
                pass
            await asyncio.to_thread(old.rename, old_renamed)
            await asyncio.to_thread(new.rename, sandbox_id)
        except APIError as exc:
            logger.error("MIGRATE CUTOVER FAILED %s: %s — rolling back to old box", sandbox_id, exc)
            try:
                await asyncio.to_thread(new.remove, force=True)
            except APIError:
                pass
            try:  # best-effort restore of the old box
                await asyncio.to_thread(old.rename, sandbox_id)
                await asyncio.to_thread(old.start)
            except APIError:
                pass
            return {"status": "failed", "sandbox_id": sandbox_id, "reason": f"cutover failed: {exc}"}

        try:
            await asyncio.to_thread(old.remove, force=True)
        except APIError as exc:
            logger.warning("migrate %s: old container cleanup failed (non-fatal): %s", sandbox_id, exc)

        try:
            sbx = await store.get(sandbox_id)
            if sbx:
                sbx.template_version = cur.version or new_ver
                sbx.container_id = new.id
                await store.save(sbx)
        except Exception as exc:
            logger.warning("migrate %s: store update failed (non-fatal): %s", sandbox_id, exc)
    finally:
        # Unlock — the new container now answers as sandbox_id; calls resume.
        await activity.release_migration(sandbox_id)

    logger.info("MIGRATED %s -> %s (version=%s)", sandbox_id, target, cur.version or new_ver)
    return {
        "status": "migrated", "sandbox_id": sandbox_id,
        "to_version": cur.version or new_ver, "to_image": target,
        "platform_env_changed": platform_env_changes,
    }


async def _restart_container(container) -> None:
    try:
        await asyncio.to_thread(container.start)
    except APIError as exc:
        logger.error("migrate(s3): could not restart old container %s: %s",
                     getattr(container, "name", "?"), exc)


async def _migrate_s3_ordered(
    sandbox_id: str, *, old, target: str, env: list, volumes: dict, labels: dict,
    host: dict, cur, store, verify_timeout: int,
) -> dict:
    """In-place migrate for an S3-backed (EC2-tier) box, ordered so no edit is
    lost. There is no shared /home/agent volume here — the home dir is S3-backed
    (hot-sync down on boot, up-sync on graceful shutdown) — so the swap MUST:

        1. drain in-flight calls, then GRACEFULLY stop the old box. Its
           shutdown.sh trap runs the full flush (cloud-files up + hot-sync up),
           so /home/agent is persisted to S3 BEFORE anything else boots.
        2. boot the new box as a FRESH boot (SANDBOX_MIGRATION stripped) so its
           entrypoint hot-syncs the just-flushed S3 down + re-pulls cloud-files.
        3. verify readiness + version, then rename old->old-ts, new->sandbox_id.
        4. remove the old box.

    On ANY failure before cutover the OLD box is restarted (it re-hydrates the
    flushed state from S3, so no data is lost) and the migration reports failed.

    GATED by settings.enable_s3_migrate. This path cannot be unit-tested without
    real S3 + an EC2 sandbox image; the server agent MUST validate it against a
    throwaway sandbox before enabling the flag. Never raises."""
    from orchestrator import activity
    from orchestrator.sandbox_manager import _get_docker_client

    client = _get_docker_client()
    tmp_name = f"{sandbox_id}-mig"
    # Fresh boot for the new box: it has no shared volume, so it must down-sync
    # everything the old box just flushed.
    env_fresh = [e for e in env if not e.startswith("SANDBOX_MIGRATION=")]

    await activity.mark_migrating(sandbox_id)
    new = None
    try:
        if not await activity.drain_inflight(sandbox_id, timeout=20.0):
            return {"status": "busy_deferred", "sandbox_id": sandbox_id,
                    "reason": "in-flight tool calls did not drain; retry later"}

        # 1. Graceful stop = full flush to S3 via the old box's shutdown trap.
        # Give docker stop headroom over the in-container shutdown budget.
        stop_timeout = settings.shutdown_timeout_seconds + 30
        try:
            await asyncio.to_thread(old.stop, timeout=stop_timeout)
        except APIError as exc:
            logger.error("migrate(s3) %s: graceful stop/flush failed: %s — old box kept", sandbox_id, exc)
            await _restart_container(old)
            return {"status": "failed", "sandbox_id": sandbox_id,
                    "reason": f"pre-migrate graceful flush failed: {exc}"}

        # clear any stale temp container
        try:
            await asyncio.to_thread(lambda: client.containers.get(tmp_name).remove(force=True))
        except NotFound:
            pass
        except APIError as exc:
            logger.warning("migrate(s3) %s: stale temp cleanup: %s", sandbox_id, exc)

        # 2. Boot the new box (fresh boot → hot-sync down from the flushed S3).
        run_kwargs: dict = dict(
            image=target, name=tmp_name, detach=True, environment=env_fresh,
            volumes=volumes or None, network=settings.docker_network,
            ports={"22/tcp": None}, extra_hosts={"host.docker.internal": "host-gateway"},
            labels=labels, restart_policy={"Name": "no", "MaximumRetryCount": 0},
        )
        template = labels.get("matrx.template")
        tier = labels.get("matrx.tier")
        run_kwargs.update(container_runtime_isolation(template, tier))
        if host.get("NanoCpus"):
            run_kwargs["nano_cpus"] = host["NanoCpus"]
        if host.get("Memory"):
            run_kwargs["mem_limit"] = host["Memory"]
        try:
            new = await asyncio.to_thread(lambda: client.containers.run(**run_kwargs))
        except APIError as exc:
            logger.error("migrate(s3) %s: new create failed: %s — restarting old", sandbox_id, exc)
            await _restart_container(old)
            return {"status": "failed", "sandbox_id": sandbox_id, "reason": f"create failed: {exc}"}

        ready = await _wait_container_ready(new, verify_timeout, template)
        new_ver = (await _container_version(new)) if ready else None
        version_ok = cur.version is None or new_ver == cur.version
        if not ready or not version_ok:
            logger.error(
                "MIGRATE(s3) FAILED %s: ready=%s ver=%s exp=%s — removing new, restarting old",
                sandbox_id, ready, new_ver, cur.version,
            )
            try:
                await asyncio.to_thread(new.remove, force=True)
            except APIError:
                pass
            await _restart_container(old)
            return {"status": "failed", "sandbox_id": sandbox_id,
                    "reason": f"new box not ready / version mismatch (ready={ready})"}

        # 3. Cutover — old is already stopped.
        old_renamed = f"{sandbox_id}-old-{int(time.time())}"
        try:
            await asyncio.to_thread(old.rename, old_renamed)
            await asyncio.to_thread(new.rename, sandbox_id)
        except APIError as exc:
            logger.error("MIGRATE(s3) CUTOVER FAILED %s: %s — removing new, restoring old", sandbox_id, exc)
            try:
                await asyncio.to_thread(new.remove, force=True)
            except APIError:
                pass
            try:
                await asyncio.to_thread(old.rename, sandbox_id)
            except APIError:
                pass
            await _restart_container(old)
            return {"status": "failed", "sandbox_id": sandbox_id, "reason": f"cutover failed: {exc}"}

        # 4. Remove the old box; the new one now answers as sandbox_id.
        try:
            await asyncio.to_thread(old.remove, force=True)
        except APIError as exc:
            logger.warning("migrate(s3) %s: old cleanup failed (non-fatal): %s", sandbox_id, exc)

        try:
            sbx = await store.get(sandbox_id)
            if sbx:
                sbx.template_version = cur.version or new_ver
                sbx.container_id = new.id
                await store.save(sbx)
        except Exception as exc:
            logger.warning("migrate(s3) %s: store update failed (non-fatal): %s", sandbox_id, exc)
    finally:
        await activity.release_migration(sandbox_id)

    logger.info("MIGRATED(s3) %s -> %s (version=%s)", sandbox_id, target, cur.version or new_ver)
    return {"status": "migrated", "sandbox_id": sandbox_id,
            "to_version": cur.version or new_ver, "to_image": target}


async def migrate_all_drifted(*, store, max_per_pass: int = 0) -> dict:
    """Migrate every live, drifted box on this host to the current image — the
    rolling auto-migrate. Sequential (one swap at a time) so we never thrash the
    host; busy boxes return ``busy_deferred`` and are simply retried on the next
    pass (the reaper calls this every sweep when MATRX_AUTO_MIGRATE=1), which is
    the "keep checking until it's idle, then migrate" behavior. ``max_per_pass``
    caps swaps per call (0 = no cap). Never raises."""
    from orchestrator.sandbox_manager import _get_docker_client
    from orchestrator.versioning import compute_drift

    results: dict[str, list[str]] = {"migrated": [], "deferred": [], "failed": [], "skipped": [], "unsupported": []}
    try:
        client = _get_docker_client()
        drifted = await asyncio.to_thread(
            lambda: [b for b in compute_drift(client) if b.drifted and b.sandbox_id]
        )
    except Exception as exc:
        logger.warning("migrate_all: drift scan failed: %s", exc)
        return {"error": str(exc), **results}

    done = 0
    for box in drifted:
        if max_per_pass and done >= max_per_pass:
            results["skipped"].append(box.sandbox_id)
            continue
        try:
            res = await migrate_sandbox(box.sandbox_id, store=store, require_idle=True)
        except Exception as exc:  # defensive — migrate_sandbox already guards, but never let one box abort the pass
            logger.error("migrate_all: %s raised: %s", box.sandbox_id, exc)
            results["failed"].append(box.sandbox_id)
            continue
        st = res.get("status")
        if st in ("migrated", "already_current"):
            results["migrated"].append(box.sandbox_id)
            done += 1
        elif st == "busy_deferred":
            results["deferred"].append(box.sandbox_id)
        elif st == "unsupported_storage":
            # S3-backed/ec2 box — safely refused (not a failure). Don't count
            # toward the per-pass cap; it'll keep being skipped until the
            # S3-sync-ordered path exists.
            results["unsupported"].append(box.sandbox_id)
        else:
            results["failed"].append(box.sandbox_id)
            done += 1
    if results["migrated"] or results["failed"]:
        logger.info(
            "migrate_all pass: migrated=%d deferred=%d failed=%d skipped=%d unsupported=%d",
            len(results["migrated"]), len(results["deferred"]),
            len(results["failed"]), len(results["skipped"]), len(results["unsupported"]),
        )
    return results
