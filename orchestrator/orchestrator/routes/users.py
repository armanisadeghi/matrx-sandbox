"""Per-user storage / persistence routes.

These power the matrx-frontend admin panel's per-user data view + the
end-user "Delete my persistent storage" action.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from orchestrator import sandbox_manager
from orchestrator.config import settings
from orchestrator.storage_layout import resolve_user_storage, user_volume_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


class MemoryPutRequest(BaseModel):
    content: str


class MemoryEntry(BaseModel):
    path: str
    content: str
    updated_at: Any | None = None


class MemoryListResponse(BaseModel):
    user_id: str
    entries: list[MemoryEntry]
    total: int


@router.get("/{user_id}/persistence")
async def get_user_persistence(user_id: str) -> dict[str, Any]:
    """Return what we know about a user's persistent storage on this orchestrator.

    Cheap — uses Docker daemon metadata + SandboxStore counts, no shell-out.
    """
    location = resolve_user_storage(user_id, tier=settings.host_tier or None)

    # Volume size only meaningful on hosted tier; EC2 tier persistence is in S3
    # and surfaced via aidream's cloud_sync system, not by this orchestrator.
    volume_bytes: int | None = None
    if location.tier == "hosted" and location.volume_name:
        volume_bytes = sandbox_manager.get_user_volume_size(user_id)

    # How many sandboxes does this user have on this tier?
    sandboxes = await sandbox_manager.list_sandboxes(user_id=user_id)
    active = sum(1 for s in sandboxes if s.status.value in ("ready", "running", "starting"))

    return {
        "user_id": user_id,
        "tier": location.tier,
        "volume_name": location.volume_name,
        "volume_bytes": volume_bytes,
        "volume_bytes_known": volume_bytes is not None,
        "s3_bucket": location.s3_bucket,
        "s3_hot_prefix": location.s3_hot_prefix,
        "s3_cold_prefix": location.s3_cold_prefix,
        "sandboxes_total": len(sandboxes),
        "sandboxes_active": active,
    }


@router.delete("/{user_id}/volume", status_code=204)
async def delete_user_volume(user_id: str) -> None:
    """Permanently delete a user's hosted-tier Docker volume.

    Refuses if any sandbox is currently using it. Returns 204 on success.
    Returns 404 if the user has no volume on this tier (no-op).

    This is destructive — the matrx-frontend should gate it behind a
    double-confirm dialog with the user typing the user_id.
    """
    if (settings.host_tier or "ec2") != "hosted":
        raise HTTPException(
            status_code=400,
            detail=(
                "Volume deletion only applies to the hosted tier. "
                "EC2-tier user data lives in S3 and is managed via cloud_sync."
            ),
        )

    try:
        ok = await sandbox_manager.delete_user_volume(user_id)
    except RuntimeError as e:
        # Container still attached — surface clearly.
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        # Bad user_id format.
        raise HTTPException(status_code=400, detail=str(e))

    if not ok:
        raise HTTPException(status_code=500, detail="Failed to delete volume")


# ── Central, cross-project per-user memory ───────────────────────────────────
# The backing store for /home/agent/.matrx/memory/. The orchestrator hydrates
# these into a sandbox on create and captures edits on graceful teardown (see
# orchestrator/memory_sync.py), so memory follows the user across every project
# and every box — including the ephemeral slim/EC2 ones that keep no volume.
# These routes also let the matrx-frontend show + edit the memory directly.

@router.get("/{user_id}/memory", response_model=MemoryListResponse)
async def list_user_memory(user_id: str) -> MemoryListResponse:
    """Return every memory entry for a user (small text files)."""
    store = sandbox_manager._get_store()
    try:
        rows = await store.memory_list(user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list memory: {e}")
    entries = [
        MemoryEntry(path=r["path"], content=r["content"], updated_at=r.get("updated_at"))
        for r in rows
    ]
    return MemoryListResponse(user_id=user_id, entries=entries, total=len(entries))


@router.put("/{user_id}/memory/{path:path}", response_model=MemoryEntry)
async def put_user_memory(user_id: str, path: str, body: MemoryPutRequest) -> MemoryEntry:
    """Upsert one memory entry at ``path`` (relative under .matrx/memory/)."""
    path = path.strip().lstrip("/")
    if not path or ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="Invalid memory path")
    store = sandbox_manager._get_store()
    try:
        await store.memory_put(user_id, path, body.content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save memory: {e}")
    return MemoryEntry(path=path, content=body.content, updated_at=None)


@router.delete("/{user_id}/memory/{path:path}", status_code=204)
async def delete_user_memory(user_id: str, path: str) -> None:
    """Delete one memory entry. 404 if it didn't exist."""
    path = path.strip().lstrip("/")
    store = sandbox_manager._get_store()
    try:
        ok = await store.memory_delete(user_id, path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete memory: {e}")
    if not ok:
        raise HTTPException(status_code=404, detail=f"No memory at '{path}'")
