"""One container-isolation policy shared by every sandbox constructor."""

from __future__ import annotations

from typing import Any

_AIDREAM_TEMPLATE = "aidream"
_AIDREAM_TMPFS = {
    "/tmp": "rw,nosuid,nodev,mode=1777",
    "/var/tmp": "rw,nosuid,nodev,mode=1777",
    "/run": "rw,nosuid,nodev,mode=1777",
    "/var/log/sandbox": "rw,nosuid,nodev,mode=0775,uid=1000,gid=1000",
    "/var/log/aidream": "rw,nosuid,nodev,mode=0775,uid=1000,gid=1000",
}


def container_runtime_isolation(template: str | None, tier: str | None) -> dict[str, Any]:
    """Return Docker run kwargs required by the selected template.

    This must be applied by cold create and every migration constructor. The
    aidream image serves certified source from its image layer, so Docker—not
    in-container ownership—must enforce a read-only root filesystem.
    """

    base: dict[str, Any] = {
        "read_only": False,
        "tmpfs": None,
        "cap_add": ["SYS_ADMIN"],
        "devices": ["/dev/fuse"],
        "cap_drop": [],
    }
    if template != _AIDREAM_TEMPLATE or tier != "hosted":
        return base
    return {
        "read_only": True,
        "tmpfs": {**_AIDREAM_TMPFS, "/data/cold": "rw,nosuid,nodev,mode=0775,uid=1000,gid=1000"},
        "cap_add": [],
        "devices": [],
        "cap_drop": [],
    }


# ``warm_pool_supports_template`` lived here until 2026-09-17, when the warm
# pool was retired (orchestrator/pool.py): NO template can be pre-warmed, not
# just the owner-bound ones, because a box that boots before it has a user and
# an organization can never be handed one afterwards.

__all__ = ["container_runtime_isolation"]
