"""Host-owned recovery state for volume-backed sandbox migrations.

This is deliberately outside sandbox config: a sandbox can write its mounted
home, but it cannot write this journal or take its host flock.  A missing or
unwritable state mount is a refusal to migrate, never a return to the unsafe
in-memory-only cutover.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

STATE_DIR = Path("/var/lib/matrx-sandbox/hosted-migrations")
_TERMINAL = {"committed", "recovered"}


class HostedMigrationStateError(RuntimeError):
    """The durable host recovery boundary is unavailable or corrupt."""


def _validate_id(sandbox_id: str) -> None:
    if not sandbox_id or "/" in sandbox_id or sandbox_id in {".", ".."}:
        raise HostedMigrationStateError("invalid sandbox id for hosted migration state")


class HostedMigrationJournal:
    def __init__(self, root: Path = STATE_DIR):
        self.root = root

    def ensure_ready(self) -> None:
        try:
            # The production location must be an explicit orchestrator-only
            # mount. A directory baked into the image is erased on recreate and
            # is unsafe for crash recovery. Test roots intentionally bypass
            # this deployment assertion.
            if self.root == STATE_DIR and not os.path.ismount(self.root):
                raise HostedMigrationStateError(
                    f"hosted migration state is not a mounted durable path: {self.root}"
                )
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o750)
            probe = self.root / ".write-probe"
            fd = os.open(probe, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                os.write(fd, b"ok")
                os.fsync(fd)
            finally:
                os.close(fd)
            probe.unlink()
        except OSError as exc:
            raise HostedMigrationStateError(
                f"hosted migration state is not durable/writable at {self.root}: {exc}"
            ) from exc

    def path_for(self, sandbox_id: str) -> Path:
        _validate_id(sandbox_id)
        return self.root / f"{sandbox_id}.json"

    def read(self, sandbox_id: str) -> dict[str, Any] | None:
        path = self.path_for(sandbox_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HostedMigrationStateError(f"corrupt hosted migration journal {path}") from exc
        if record.get("sandbox_id") != sandbox_id or not record.get("phase"):
            raise HostedMigrationStateError(f"invalid hosted migration journal {path}")
        return record

    def write(self, record: dict[str, Any]) -> None:
        sandbox_id = str(record.get("sandbox_id") or "")
        path = self.path_for(sandbox_id)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{sandbox_id}.", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temp_name, path)
            dir_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    @contextmanager
    def lock(self, sandbox_id: str) -> Iterator[None]:
        self.ensure_ready()
        _validate_id(sandbox_id)
        lock_path = self.root / f"{sandbox_id}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if not _try_flock(fd):
                raise HostedMigrationStateError("hosted migration already in progress")
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def inflight(self, sandbox_id: str) -> bool:
        record = self.read(sandbox_id)
        return bool(record and record.get("phase") not in _TERMINAL)

    def pending(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            sandbox_id = path.stem
            record = self.read(sandbox_id)
            if record and record.get("phase") not in _TERMINAL:
                records.append(record)
        return records


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def manifest_digest(manifest: bytes) -> str:
    """Digest a host-generated external manifest, never client supplied state."""
    return hashlib.sha256(manifest).hexdigest()


async def snapshot_volume(client, *, volume: str, backup_volume: str, image: str) -> str:
    """Copy a quiesced home volume into a distinct host-owned backup volume.

    GNU tar's numeric-owner/xattr/ACL/sparse flags are intentional: a simple
    ``docker cp`` loses the very metadata a migration promises to preserve.
    The helper container is ephemeral; the backup volume is retained until a
    committed store readback proves the replacement route.
    """
    import asyncio
    from docker.errors import APIError
    command = (
        "set -eu; rm -rf /backup/* /backup/.[!.]* /backup/..?* 2>/dev/null || true; "
        "cd /source; tar --create --file=- --numeric-owner --acls --xattrs --sparse . "
        "| tar --extract --file=- --numeric-owner --acls --xattrs --sparse -C /backup"
    )
    try:
        await asyncio.to_thread(
            client.containers.run, image, ["/bin/sh", "-ec", command], remove=True,
            volumes={volume: {"bind": "/source", "mode": "ro"}, backup_volume: {"bind": "/backup", "mode": "rw"}},
        )
    except APIError as exc:
        raise HostedMigrationStateError(f"hosted volume backup failed: {exc}") from exc
    return backup_volume


async def restore_volume(client, *, volume: str, backup_volume: str, image: str) -> None:
    """Restore backup before old-container restart; failures remain recoverable."""
    import asyncio
    from docker.errors import APIError
    command = (
        "set -eu; rm -rf /target/* /target/.[!.]* /target/..?* 2>/dev/null || true; "
        "cd /backup; tar --create --file=- --numeric-owner --acls --xattrs --sparse . "
        "| tar --extract --file=- --numeric-owner --acls --xattrs --sparse -C /target"
    )
    try:
        await asyncio.to_thread(
            client.containers.run, image, ["/bin/sh", "-ec", command], remove=True,
            volumes={backup_volume: {"bind": "/backup", "mode": "ro"}, volume: {"bind": "/target", "mode": "rw"}},
        )
    except APIError as exc:
        raise HostedMigrationStateError(f"hosted volume restore failed: {exc}") from exc
