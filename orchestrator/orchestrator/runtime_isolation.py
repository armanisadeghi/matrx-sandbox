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


def warm_pool_supports_template(template: str | None) -> bool:
    """Heavy aidream boxes require owner env/volume and cannot be pre-warmed."""

    return template != _AIDREAM_TEMPLATE


__all__ = ["container_runtime_isolation", "warm_pool_supports_template"]
