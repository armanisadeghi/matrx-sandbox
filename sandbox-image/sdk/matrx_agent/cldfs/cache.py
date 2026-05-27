"""Local byte cache for cldfs reads — file-id + version keyed, LRU evicted.

Why a cache at all when same-region S3 is ~10ms? Because:
- Multiple ``read(fd, offset, n)`` calls per ``open()`` are common and we
  don't want to re-fetch a 5MB PDF for each.
- The Linux VFS layer already does some caching but it's per-FUSE-handle;
  this cache survives close/reopen.
- Disk space is cheap; per-version keying means we never serve a stale file
  (a new version writes a new cache entry).

Bound the cache by total bytes — defaults to 2 GiB, configurable via the
``MATRX_CLDFS_CACHE_BYTES`` env var. LRU eviction when the budget is hit.
"""

from __future__ import annotations

import os
import shutil
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Optional

DEFAULT_CACHE_DIR = Path(
    os.environ.get("MATRX_CLDFS_CACHE_DIR") or "/tmp/cldfs-cache"
)
DEFAULT_CACHE_BYTES = int(
    os.environ.get("MATRX_CLDFS_CACHE_BYTES") or 2 * 1024 * 1024 * 1024
)


class ByteCache:
    """File-id + version keyed read cache. Synchronous; serialises writers
    via a single Lock — the FUSE handler runs ops on a small thread pool so
    contention is light."""

    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        max_bytes: int = DEFAULT_CACHE_BYTES,
    ) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max_bytes
        self._lock = Lock()
        # key = "<file_id>-v<version>" → bytes-on-disk
        self._lru: "OrderedDict[str, int]" = OrderedDict()
        self._total_bytes = 0
        self._scan_existing()

    # ── Public ────────────────────────────────────────────────────────────

    def get_path(self, file_id: str, version: int) -> Optional[Path]:
        """Return the cached file path if present, else None.

        Touching the LRU bump is intentional: every successful read renews
        the entry's freshness."""
        key = self._key(file_id, version)
        with self._lock:
            if key not in self._lru:
                return None
            self._lru.move_to_end(key)
        return self._path_for(key)

    def put_path(self, file_id: str, version: int) -> Path:
        """Return the path the caller should write to. Caller is expected to
        write atomically (write-then-rename) and then call ``commit()``."""
        key = self._key(file_id, version)
        return self._path_for(key)

    def commit(self, file_id: str, version: int) -> None:
        """Register a freshly-written cache entry.

        Called after the caller has written bytes to ``put_path(...)`` and
        the file is durable. Updates the LRU + total-bytes counter and
        evicts to budget if needed."""
        key = self._key(file_id, version)
        path = self._path_for(key)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        with self._lock:
            if key in self._lru:
                self._total_bytes -= self._lru[key]
            self._lru[key] = size
            self._lru.move_to_end(key)
            self._total_bytes += size
            self._evict_until_under_budget()

    def invalidate(self, file_id: str) -> None:
        """Drop every cached version of a file. Called on rename or delete."""
        with self._lock:
            stale: list[str] = [k for k in self._lru if k.startswith(f"{file_id}-v")]
            for key in stale:
                self._total_bytes -= self._lru.pop(key)
                try:
                    self._path_for(key).unlink()
                except FileNotFoundError:
                    pass

    def clear(self) -> None:
        with self._lock:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir.mkdir(parents=True, exist_ok=True)
            self._lru.clear()
            self._total_bytes = 0

    # ── Internals ─────────────────────────────────────────────────────────

    def _key(self, file_id: str, version: int) -> str:
        return f"{file_id}-v{version}"

    def _path_for(self, key: str) -> Path:
        # SQLite-safe key chars + Linux fs-safe → just use the key verbatim.
        return self._dir / key

    def _scan_existing(self) -> None:
        """Re-seed the LRU from disk on init. Order is by mtime (oldest
        first) — close enough to true LRU for boot-time recovery."""
        entries = sorted(
            (p for p in self._dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        for p in entries:
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                continue
            self._lru[p.name] = size
            self._total_bytes += size
        self._evict_until_under_budget()

    def _evict_until_under_budget(self) -> None:
        while self._total_bytes > self._max_bytes and self._lru:
            oldest_key, size = self._lru.popitem(last=False)
            self._total_bytes -= size
            try:
                self._path_for(oldest_key).unlink()
            except FileNotFoundError:
                pass


__all__ = ["ByteCache", "DEFAULT_CACHE_DIR", "DEFAULT_CACHE_BYTES"]
