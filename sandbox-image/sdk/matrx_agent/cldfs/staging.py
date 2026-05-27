"""Write staging — buffer in-flight writes locally, commit on release.

The FUSE contract gives us `write(fd, offset, data)` callbacks with no
guarantee about when the file is "done." We can't POST every write to the
bridge — that would create one cld_files version per fwrite. Instead:

1. ``open(O_WRONLY)`` allocates a staging file.
2. Every ``write(fd, ...)`` appends to it.
3. ``flush()`` is a no-op (Python's `flush` is not "commit", it's "sync OS buffers").
4. ``release(fd)`` (close) reads the final staging file and POSTs it to
   ``/api/cloud-files/put``. The bridge creates a new version.

This matches cld_files versioning intent: a commit per "the agent closed
the file," not per fwrite.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

DEFAULT_STAGING_DIR = Path(
    os.environ.get("MATRX_CLDFS_STAGING_DIR") or "/tmp/cldfs-staging"
)


@dataclass
class StagedFile:
    """Handle to an in-flight write."""

    handle_id: str
    file_path: str
    staging_path: Path
    fd: int


class WriteStaging:
    """Tracks open write handles and their on-disk staging files."""

    def __init__(self, staging_dir: Path = DEFAULT_STAGING_DIR) -> None:
        staging_dir.mkdir(parents=True, exist_ok=True)
        self._dir = staging_dir
        self._lock = Lock()
        self._handles: dict[str, StagedFile] = {}

    def open_for_write(self, file_path: str) -> StagedFile:
        handle_id = uuid.uuid4().hex
        # tempfile.mkstemp returns (fd, path) and guarantees uniqueness.
        fd, raw_path = tempfile.mkstemp(prefix=f"{handle_id}-", dir=str(self._dir))
        staged = StagedFile(
            handle_id=handle_id,
            file_path=file_path,
            staging_path=Path(raw_path),
            fd=fd,
        )
        with self._lock:
            self._handles[handle_id] = staged
        return staged

    def get(self, handle_id: str) -> Optional[StagedFile]:
        with self._lock:
            return self._handles.get(handle_id)

    def write_at(self, handle_id: str, offset: int, data: bytes) -> int:
        """Mirror of os.pwrite onto the staging fd. Returns bytes written."""
        staged = self.get(handle_id)
        if staged is None:
            raise OSError(f"unknown handle {handle_id}")
        return os.pwrite(staged.fd, data, offset)

    def read_back(self, handle_id: str) -> bytes:
        """Read the full staging file. Called on release() to ship to bridge."""
        staged = self.get(handle_id)
        if staged is None:
            raise OSError(f"unknown handle {handle_id}")
        os.fsync(staged.fd)
        return staged.staging_path.read_bytes()

    def close(self, handle_id: str) -> Optional[StagedFile]:
        """Close the staging fd and remove the entry. Returns the StagedFile
        so the caller can ship its bytes."""
        with self._lock:
            staged = self._handles.pop(handle_id, None)
        if staged is None:
            return None
        try:
            os.close(staged.fd)
        except OSError:
            pass
        return staged

    def discard(self, handle_id: str) -> None:
        """Drop a staging file without committing. Used on write errors."""
        staged = self.close(handle_id)
        if staged is not None:
            try:
                staged.staging_path.unlink()
            except FileNotFoundError:
                pass


__all__ = ["StagedFile", "WriteStaging", "DEFAULT_STAGING_DIR"]
