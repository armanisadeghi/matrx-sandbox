"""FUSE handler — the bridge between Linux VFS ops and cldfs.

``CldfsHandler`` is the abstract handler — it implements the operations in
terms of three injected dependencies (``MetadataMirror`` for path → row
resolution, ``ByteCache`` for read bytes, ``WriteStaging`` for in-flight
writes) plus an injected ``BridgeClient`` for the actual REST calls to
``/api/cloud-files/*``.

The pyfuse3 binding is wired in `mount.py`; this file deliberately keeps
that import deferred so the SDK ships without pyfuse3 as a hard dep until
cldfs is the production path.

Operation summary:

| FUSE op           | What it does                                             |
|-------------------|----------------------------------------------------------|
| lookup            | MetadataMirror.lookup(path)                              |
| getattr           | size / mtime from MetadataMirror row                     |
| readdir           | MetadataMirror.list_dir(path) → group by next path seg   |
| open (read)       | ByteCache.get → fetch via bridge.get on miss             |
| read              | pread on cached bytes                                    |
| open (write)      | WriteStaging.open_for_write                              |
| write             | WriteStaging.write_at                                    |
| release (write)   | bridge.put(staging_bytes) → invalidate cache, mirror.upsert
| unlink            | bridge.delete(path) → mirror.delete_by_path             |
| rename            | bridge.move(from, to) → mirror.rename                   |
"""

from __future__ import annotations

import errno
import os
import time
from dataclasses import dataclass
from typing import Protocol

from matrx_agent.cldfs.cache import ByteCache
from matrx_agent.cldfs.mirror import FileRow, MetadataMirror
from matrx_agent.cldfs.staging import WriteStaging


class BridgeClient(Protocol):
    """Subset of ``matrx_agent.cloud_sync.client.AsyncBridgeClient`` that
    cldfs talks through. Kept as a Protocol so tests can substitute a
    fake without dragging the real client + httpx into the test."""

    async def get_bytes(self, file_path: str) -> bytes: ...
    async def put_bytes(self, file_path: str, data: bytes) -> FileRow: ...
    async def delete_path(self, file_path: str) -> None: ...
    async def move_path(self, from_path: str, to_path: str) -> FileRow: ...


@dataclass(frozen=True)
class Stat:
    """POSIX-like stat block returned by getattr / lookup."""

    is_dir: bool
    size: int
    mtime: float
    mode: int  # POSIX mode bits


# Default mode bits — files are 0o644, dirs are 0o755. Writable bit is
# present so the agent can edit; permission to actually mutate is gated by
# the cld_files RLS / ownership check upstream, not the local mode bit.
DEFAULT_FILE_MODE = 0o100644  # S_IFREG | 0644
DEFAULT_DIR_MODE = 0o040755   # S_IFDIR | 0755


class CldfsHandler:
    """Pure-Python handler. The pyfuse3 adapter in ``mount.py`` translates
    pyfuse3 ops → these methods. Keeping the handler binding-agnostic means
    we could swap to ``fusepy`` or a Rust binding later without touching
    handler logic."""

    def __init__(
        self,
        mirror: MetadataMirror,
        cache: ByteCache,
        staging: WriteStaging,
        bridge: BridgeClient,
    ) -> None:
        self._mirror = mirror
        self._cache = cache
        self._staging = staging
        self._bridge = bridge

    # ── Metadata ───────────────────────────────────────────────────────────

    def stat_path(self, path: str) -> Stat:
        # Root + implicit directories.
        if path in ("", "/"):
            return Stat(is_dir=True, size=0, mtime=time.time(), mode=DEFAULT_DIR_MODE)
        clean = path.lstrip("/")
        row = self._mirror.lookup(clean)
        if row is not None:
            return Stat(
                is_dir=False,
                size=row.file_size,
                mtime=row.mtime,
                mode=DEFAULT_FILE_MODE,
            )
        # Implicit dir? — any file path that starts with ``clean + '/'`` makes
        # this an existing directory.
        prefix = clean.rstrip("/") + "/"
        any_match = next(self._mirror.list_dir(prefix), None)
        if any_match is not None:
            return Stat(is_dir=True, size=0, mtime=time.time(), mode=DEFAULT_DIR_MODE)
        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), path)

    def list_dir_entries(self, path: str) -> list[tuple[str, Stat]]:
        """Return ``[(name, stat), ...]`` for a directory.

        Synthesises sub-directory entries from path prefixes — cld_files has
        no directory rows. ``foo/bar.txt`` + ``foo/baz/qux.txt`` under root
        produces ``foo/`` once."""
        clean = path.strip("/")
        prefix = (clean + "/") if clean else ""
        seen_dirs: dict[str, Stat] = {}
        entries: list[tuple[str, Stat]] = []
        for row in self._mirror.list_dir(clean):
            rel = row.file_path[len(prefix):] if prefix else row.file_path
            if "/" in rel:
                top = rel.split("/", 1)[0]
                if top not in seen_dirs:
                    seen_dirs[top] = Stat(
                        is_dir=True, size=0, mtime=row.mtime, mode=DEFAULT_DIR_MODE,
                    )
            else:
                entries.append(
                    (rel, Stat(
                        is_dir=False,
                        size=row.file_size,
                        mtime=row.mtime,
                        mode=DEFAULT_FILE_MODE,
                    )),
                )
        return [*[(name, s) for name, s in seen_dirs.items()], *entries]

    # ── Read ──────────────────────────────────────────────────────────────

    async def read_full(self, path: str) -> bytes:
        clean = path.lstrip("/")
        row = self._mirror.lookup(clean)
        if row is None:
            raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        cached = self._cache.get_path(row.file_id, row.current_version)
        if cached is not None:
            return cached.read_bytes()
        data = await self._bridge.get_bytes(clean)
        target = self._cache.put_path(row.file_id, row.current_version)
        target.write_bytes(data)
        self._cache.commit(row.file_id, row.current_version)
        return data

    async def read_at(self, path: str, offset: int, n: int) -> bytes:
        full = await self.read_full(path)
        return full[offset : offset + n]

    # ── Write ─────────────────────────────────────────────────────────────

    def open_for_write(self, path: str) -> str:
        """Returns a handle the FUSE adapter stashes as its file handle."""
        clean = path.lstrip("/")
        staged = self._staging.open_for_write(clean)
        return staged.handle_id

    def write_at(self, handle_id: str, offset: int, data: bytes) -> int:
        return self._staging.write_at(handle_id, offset, data)

    async def commit_write(self, handle_id: str) -> FileRow:
        staged = self._staging.get(handle_id)
        if staged is None:
            raise OSError(errno.EBADF, "unknown handle")
        data = self._staging.read_back(handle_id)
        try:
            row = await self._bridge.put_bytes(staged.file_path, data)
        except Exception:
            # Don't drop staging — keeps the bytes around for retry / debug.
            raise
        self._mirror.upsert(row)
        # Pre-warm the cache with the bytes we just wrote.
        target = self._cache.put_path(row.file_id, row.current_version)
        target.write_bytes(data)
        self._cache.commit(row.file_id, row.current_version)
        # Close the staging fd; the staging file itself is gc'd by the OS
        # when the next reboot cleans /tmp.
        self._staging.close(handle_id)
        return row

    # ── Delete / rename ───────────────────────────────────────────────────

    async def unlink(self, path: str) -> None:
        clean = path.lstrip("/")
        row = self._mirror.lookup(clean)
        if row is None:
            raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        await self._bridge.delete_path(clean)
        self._mirror.delete(row.file_id)
        self._cache.invalidate(row.file_id)

    async def rename(self, old_path: str, new_path: str) -> None:
        old_clean = old_path.lstrip("/")
        new_clean = new_path.lstrip("/")
        row = self._mirror.lookup(old_clean)
        if row is None:
            raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), old_path)
        try:
            await self._bridge.move_path(old_clean, new_clean)
        except NotImplementedError:
            # Fallback for bridges that don't yet ship /move: get + put + delete.
            data = await self._bridge.get_bytes(old_clean)
            await self._bridge.put_bytes(new_clean, data)
            await self._bridge.delete_path(old_clean)
        self._mirror.rename(old_clean, new_clean)


__all__ = ["BridgeClient", "CldfsHandler", "Stat", "DEFAULT_FILE_MODE", "DEFAULT_DIR_MODE"]
