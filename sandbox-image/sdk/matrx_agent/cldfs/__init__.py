"""cldfs — cloud-files as a live filesystem in the sandbox.

Design + rationale: see ``docs/CLDFS_DESIGN.md`` in the matrx-sandbox repo.

Status: SCAFFOLDING. The FUSE handler, mirror, cache, and staging pieces
have public signatures locked in, but the FUSE wire (pyfuse3) is not yet
imported at module load — the SDK ships without ``pyfuse3`` as a hard
dependency. ``mount()`` raises a clean error if the binding is missing.

Phase 6a ships ``MATRX_CLOUD_FILES_LAZY=1`` (env-flag in
``cloud-files-sync.sh``) which skips the eager boot copy and lets the
existing ``CloudFilesWatcher`` keep ``~/cloud-files/`` fresh via Realtime.
That bridges to the world where cldfs is the production path."""

from __future__ import annotations

from matrx_agent.cldfs.cache import ByteCache
from matrx_agent.cldfs.handler import CldfsHandler
from matrx_agent.cldfs.mirror import MetadataMirror
from matrx_agent.cldfs.mount import Config, mount
from matrx_agent.cldfs.staging import WriteStaging

__all__ = [
    "ByteCache",
    "CldfsHandler",
    "Config",
    "MetadataMirror",
    "WriteStaging",
    "mount",
]
