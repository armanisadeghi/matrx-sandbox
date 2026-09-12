"""Server-derived lock identities, independent of the sandbox hosting tier."""
from __future__ import annotations

import hashlib

from orchestrator.config import settings
from orchestrator.storage_layout import user_volume_name


def home_key(sandbox) -> str:
    reference = getattr(sandbox, "persistence_volume", None)
    if reference:
        if reference.startswith("host:"):
            return "bind-" + hashlib.sha256(reference.encode()).hexdigest()
        return reference
    tier = getattr(sandbox, "tier", None) or settings.host_tier
    tier = getattr(tier, "value", tier)
    if tier == "hosted":
        return user_volume_name(sandbox.user_id)
    if tier == "ec2" and getattr(sandbox, "sandbox_id", None):
        return "layer-" + sandbox.sandbox_id
    raise ValueError("sandbox has no authoritative home identity")
