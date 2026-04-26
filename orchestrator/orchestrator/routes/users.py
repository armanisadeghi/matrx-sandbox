"""Per-user storage / persistence routes.

These power the matrx-frontend admin panel's per-user data view + the
end-user "Delete my persistent storage" action.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from orchestrator import sandbox_manager
from orchestrator.config import settings
from orchestrator.storage_layout import resolve_user_storage, user_volume_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


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
