"""cldfs mount entrypoint.

Production wiring (when pyfuse3 is installed):

    from matrx_agent.cldfs import mount, Config
    await mount(Config(
        mount_point="/home/agent/cloud",
        user_id=os.environ["USER_ID"],
    ))

Until pyfuse3 ships in the image, this function raises with a clear message
pointing operators at the design doc. The rest of the cldfs package is
fully usable without pyfuse3 — tests + tooling can exercise the handler /
mirror / cache / staging without ever touching FUSE.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Static configuration for a cldfs mount."""

    mount_point: str
    """Where in the sandbox filesystem cldfs is exposed. Default
    `/home/agent/cloud/`. Created if missing."""

    user_id: str
    """Owner whose cld_files to expose. Sourced from the orchestrator-injected
    ``USER_ID`` env var by default."""

    mirror_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("MATRX_CLDFS_MIRROR_PATH") or "/var/lib/cldfs/mirror.db"
        )
    )

    cache_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("MATRX_CLDFS_CACHE_DIR") or "/tmp/cldfs-cache"
        )
    )

    cache_bytes: int = field(
        default_factory=lambda: int(
            os.environ.get("MATRX_CLDFS_CACHE_BYTES") or 2 * 1024 * 1024 * 1024
        ),
    )

    staging_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("MATRX_CLDFS_STAGING_DIR") or "/tmp/cldfs-staging"
        )
    )

    read_only: bool = False
    """When True, every write op returns EROFS. The Phase 6b default is
    False per the user's decision; flip this to True for an extra-cautious
    deployment (e.g. shared agents that should never mutate a user's
    cloud-files)."""


async def mount(cfg: Config) -> None:
    """Mount cldfs at ``cfg.mount_point`` and run the FUSE event loop.

    Returns when the mount is unmounted (SIGINT, ``fusermount -u``, etc.).
    This is the only function in the cldfs package that imports pyfuse3 —
    everywhere else can be exercised without the FUSE binding installed."""
    try:
        import pyfuse3  # noqa: F401 — imported lazily so the SDK ships without the dep
    except ImportError as exc:
        raise RuntimeError(
            "cldfs requires `pyfuse3` and the container must be started with "
            "`--cap-add SYS_ADMIN --device /dev/fuse`. See "
            "docs/CLDFS_DESIGN.md for the rollout checklist."
        ) from exc

    raise NotImplementedError(
        "cldfs.mount() is scaffolding only as of 2026-05-27. Production "
        "wiring lands in Phase 6b. Use MATRX_CLOUD_FILES_LAZY=1 (Phase 6a) "
        "for the bridge UX until then."
    )


__all__ = ["Config", "mount"]
