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
import time

from docker.errors import APIError, NotFound

from orchestrator.config import settings
from orchestrator.versioning import current_image

logger = logging.getLogger(__name__)


def _binds_to_volumes(host_config: dict) -> dict:
    """Rebuild docker-py's ``volumes=`` dict from a container's HostConfig.Binds."""
    out: dict = {}
    for b in host_config.get("Binds") or []:
        parts = b.split(":")
        if len(parts) >= 2:
            out[parts[0]] = {"bind": parts[1], "mode": parts[2] if len(parts) > 2 else "rw"}
    return out


async def _wait_container_ready(container, timeout: int) -> bool:
    elapsed, interval = 0, 2
    while elapsed < timeout:
        try:
            container.reload()
            if container.status == "exited":
                return False
            code, _ = container.exec_run("test -f /tmp/.sandbox_ready")
            if code == 0:
                return True
        except (NotFound, APIError) as exc:
            logger.warning("migrate: readiness poll error: %s", exc)
            return False
        await asyncio.sleep(interval)
        elapsed += interval
    return False


def _container_version(container) -> str | None:
    """Read /etc/sandbox-image-version from inside the box (the image-baked file,
    not the overridable env) — the source of truth for what version it came up on."""
    try:
        code, out = container.exec_run("cat /etc/sandbox-image-version")
        if code == 0:
            return (out or b"").decode("utf-8", errors="replace").strip() or None
    except (NotFound, APIError):
        pass
    return None


async def migrate_sandbox(sandbox_id: str, *, store, target_image: str | None = None,
                          verify_timeout: int = 90) -> dict:
    """Migrate one box to the current image (or an explicit target_image).
    Returns a status dict; never raises."""
    from orchestrator.sandbox_manager import _get_docker_client

    client = _get_docker_client()
    try:
        old = client.containers.get(sandbox_id)
    except NotFound:
        return {"status": "not_found", "sandbox_id": sandbox_id}

    labels = old.labels or {}
    template = labels.get("matrx.template")
    cur = current_image(client, template)
    target = target_image or cur.tag
    old_image_id = (old.attrs or {}).get("Image")

    if not target_image and cur.image_id and old_image_id == cur.image_id:
        return {"status": "already_current", "sandbox_id": sandbox_id, "version": cur.version}

    cfg = old.attrs.get("Config") or {}
    host = old.attrs.get("HostConfig") or {}
    # Copy the old env EXCEPT MATRX_IMAGE_VERSION — the new image must report its
    # OWN baked version (otherwise the box would advertise the stale version and
    # the very drift we just fixed would look unfixed).
    env = [e for e in (cfg.get("Env") or []) if not e.startswith("MATRX_IMAGE_VERSION=")]
    volumes = _binds_to_volumes(host)
    tmp_name = f"{sandbox_id}-mig"

    try:
        client.containers.get(tmp_name).remove(force=True)  # clear any stale temp
    except NotFound:
        pass
    except APIError as exc:
        logger.warning("migrate %s: could not clear stale temp container: %s", sandbox_id, exc)

    logger.info(
        "migrate %s: %s -> %s (template=%s)",
        sandbox_id, (old_image_id or "?")[:19], target, template,
    )
    run_kwargs: dict = dict(
        image=target, name=tmp_name, detach=True, environment=env,
        volumes=volumes or None, network=settings.docker_network,
        cap_add=["SYS_ADMIN"], devices=["/dev/fuse"], cap_drop=[],
        ports={"22/tcp": None}, extra_hosts={"host.docker.internal": "host-gateway"},
        labels=labels, restart_policy={"Name": "no", "MaximumRetryCount": 0},
    )
    if host.get("NanoCpus"):
        run_kwargs["nano_cpus"] = host["NanoCpus"]
    if host.get("Memory"):
        run_kwargs["mem_limit"] = host["Memory"]

    try:
        new = client.containers.run(**run_kwargs)
    except APIError as exc:
        logger.error("migrate %s: new container create failed: %s", sandbox_id, exc)
        return {"status": "failed", "sandbox_id": sandbox_id, "reason": f"create failed: {exc}"}

    ready = await _wait_container_ready(new, verify_timeout)
    new_ver = _container_version(new) if ready else None
    # Skip the strict version equality when the current image is itself
    # unversioned (built before the stamp) — fall back to readiness + image id.
    version_ok = cur.version is None or new_ver == cur.version
    if not ready or not version_ok:
        logger.error(
            "MIGRATE FAILED %s: ready=%s new_version=%s expected=%s — OLD box kept running, ALARM",
            sandbox_id, ready, new_ver, cur.version,
        )
        try:
            new.remove(force=True)
        except APIError:
            pass
        return {
            "status": "failed", "sandbox_id": sandbox_id,
            "reason": f"new box not ready / version mismatch (ready={ready}, version={new_ver}->{cur.version})",
        }

    # ── Atomic cutover ───────────────────────────────────────────────────────
    old_renamed = f"{sandbox_id}-old-{int(time.time())}"
    try:
        try:
            old.stop(timeout=settings.shutdown_timeout_seconds)
        except APIError:
            pass
        old.rename(old_renamed)
        new.rename(sandbox_id)
    except APIError as exc:
        logger.error("MIGRATE CUTOVER FAILED %s: %s — rolling back to old box", sandbox_id, exc)
        try:
            new.remove(force=True)
        except APIError:
            pass
        try:  # best-effort restore of the old box
            old.rename(sandbox_id)
            old.start()
        except APIError:
            pass
        return {"status": "failed", "sandbox_id": sandbox_id, "reason": f"cutover failed: {exc}"}

    try:
        old.remove(force=True)
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

    logger.info("MIGRATED %s -> %s (version=%s)", sandbox_id, target, cur.version or new_ver)
    return {
        "status": "migrated", "sandbox_id": sandbox_id,
        "to_version": cur.version or new_ver, "to_image": target,
    }
