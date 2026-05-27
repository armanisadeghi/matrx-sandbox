"""SQLite-backed mirror of the user's cld_files row set.

This is what backs every FUSE metadata operation (lookup / getattr / readdir).
Bytes live in S3 (fetched on demand by ``ByteCache``); metadata lives here so
those ops are sub-millisecond.

Population strategy:
  1. **Cold boot** — full bootstrap from ``GET /api/cloud-files/list``.
  2. **Live updates** — ``CloudFilesWatcher.RealtimeSubscriber`` (or its
     polling fallback) calls ``upsert()`` / ``delete()`` on every cld_files
     INSERT / UPDATE / DELETE event filtered by the user.
  3. **Cache miss** — if a FUSE op asks about a path we don't have, we
     re-query the bridge for just that path before returning ENOENT.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_MIRROR_PATH = Path(
    os.environ.get("MATRX_CLDFS_MIRROR_PATH") or "/var/lib/cldfs/mirror.db"
)


@dataclass(frozen=True)
class FileRow:
    """One cld_files entry, projected to the columns cldfs cares about."""

    file_id: str
    file_path: str
    file_size: int
    mime_type: Optional[str]
    current_version: int
    etag: Optional[str]
    mtime: float
    owner_id: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id         TEXT PRIMARY KEY,
    file_path       TEXT NOT NULL UNIQUE,
    file_size       INTEGER NOT NULL,
    mime_type       TEXT,
    current_version INTEGER NOT NULL,
    etag            TEXT,
    mtime           REAL NOT NULL,
    owner_id        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(file_path);
"""


class MetadataMirror:
    """Thread-safe wrapper over the SQLite mirror DB.

    Every method is sync — SQLite is fast enough for the per-FUSE-op metadata
    lookups and the Realtime subscriber's per-event upserts. Async wrapping
    would only buy us cooperative scheduling, not real parallelism, and the
    FUSE bindings ultimately need a sync answer."""

    def __init__(self, path: Path = DEFAULT_MIRROR_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.row_factory = sqlite3.Row

    # ── Read ───────────────────────────────────────────────────────────────

    def lookup(self, file_path: str) -> Optional[FileRow]:
        cur = self._conn.execute(
            "SELECT * FROM files WHERE file_path = ?", (file_path,)
        )
        row = cur.fetchone()
        return _row_to_dataclass(row) if row else None

    def lookup_by_id(self, file_id: str) -> Optional[FileRow]:
        cur = self._conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (file_id,)
        )
        row = cur.fetchone()
        return _row_to_dataclass(row) if row else None

    def list_dir(self, dir_path: str) -> Iterator[FileRow]:
        """Iterate every file whose parent directory is ``dir_path``.

        Dir-implicit semantics: cld_files has no rows for directories — they
        exist only as substrings of file paths. ``list_dir("notes/")`` yields
        every file at exactly that depth; sub-dirs are surfaced by examining
        prefixes (the FUSE handler does that grouping)."""
        prefix = dir_path.rstrip("/") + "/" if dir_path else ""
        cur = self._conn.execute(
            "SELECT * FROM files WHERE file_path GLOB ? ORDER BY file_path",
            (f"{prefix}*",),
        )
        for row in cur:
            yield _row_to_dataclass(row)

    # ── Write (called by the Realtime subscriber + bridge writes) ──────────

    def upsert(self, row: FileRow) -> None:
        self._conn.execute(
            """
            INSERT INTO files (file_id, file_path, file_size, mime_type,
                               current_version, etag, mtime, owner_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                file_path       = excluded.file_path,
                file_size       = excluded.file_size,
                mime_type       = excluded.mime_type,
                current_version = excluded.current_version,
                etag            = excluded.etag,
                mtime           = excluded.mtime
            """,
            (
                row.file_id,
                row.file_path,
                row.file_size,
                row.mime_type,
                row.current_version,
                row.etag,
                row.mtime,
                row.owner_id,
            ),
        )
        self._conn.commit()

    def delete(self, file_id: str) -> None:
        self._conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
        self._conn.commit()

    def delete_by_path(self, file_path: str) -> None:
        self._conn.execute("DELETE FROM files WHERE file_path = ?", (file_path,))
        self._conn.commit()

    def rename(self, old_path: str, new_path: str) -> None:
        self._conn.execute(
            "UPDATE files SET file_path = ?, mtime = ? WHERE file_path = ?",
            (new_path, time.time(), old_path),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _row_to_dataclass(row: sqlite3.Row) -> FileRow:
    return FileRow(
        file_id=row["file_id"],
        file_path=row["file_path"],
        file_size=row["file_size"],
        mime_type=row["mime_type"],
        current_version=row["current_version"],
        etag=row["etag"],
        mtime=row["mtime"],
        owner_id=row["owner_id"],
    )


__all__ = ["FileRow", "MetadataMirror", "DEFAULT_MIRROR_PATH"]
