"""Descriptor-safe deployment lease over the hosted migration lock set.

The deployers run this helper as the same identity as the orchestrator service.
It creates only explicitly derived locks, validates every lock inode in the
journal directory, acquires the complete set, and holds the descriptors until
its stdin is closed.  Keeping the descriptors in one process avoids the
lstat/open and cross-UID ownership gaps that shell redirections introduce.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import BinaryIO

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")


class LockInventoryError(RuntimeError):
    """The durable lock namespace is malformed or changed during admission."""


class LockContentionError(RuntimeError):
    """A migration or another deployment currently owns one of the locks."""


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        stat.S_IMODE(value.st_mode),
    )


class LockLease:
    def __init__(self, root: Path):
        self.root = root
        self.uid = os.geteuid()
        self.gid = os.getegid()
        self._directory_fd: int | None = None
        self._directory_identity: tuple[int, int] | None = None
        self._lock_fds: list[int] = []

    def _directory(self) -> int:
        if self._directory_fd is None:
            try:
                self._directory_fd = os.open(
                    self.root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
            except OSError as exc:
                raise LockInventoryError(f"cannot open migration journal directory: {exc}") from exc
            opened = os.fstat(self._directory_fd)
            self._directory_identity = (opened.st_dev, opened.st_ino)
        return self._directory_fd

    def _validate_directory_identity(self) -> None:
        directory_fd = self._directory()
        opened = os.fstat(directory_fd)
        try:
            named = os.stat(self.root, follow_symlinks=False)
        except OSError as exc:
            raise LockInventoryError(f"migration journal directory changed: {exc}") from exc
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or identity != (named.st_dev, named.st_ino)
            or identity != self._directory_identity
        ):
            raise LockInventoryError("migration journal directory inode changed")

    def _validate(self, name: str, value: os.stat_result) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise LockInventoryError(f"migration lock is not a regular file: {name}")
        if value.st_uid != self.uid or value.st_gid != self.gid:
            raise LockInventoryError(f"migration lock has incompatible owner: {name}")
        if stat.S_IMODE(value.st_mode) != 0o600:
            raise LockInventoryError(f"migration lock has incompatible mode: {name}")
        if value.st_nlink != 1:
            raise LockInventoryError(f"migration lock has unexpected hard links: {name}")

    def _snapshot(
        self, names: set[str] | None = None
    ) -> dict[str, tuple[int, int, int, int, int]]:
        self._validate_directory_identity()
        directory_fd = self._directory()
        result: dict[str, tuple[int, int, int, int, int]] = {}
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if not entry.name.endswith(".lock"):
                    continue
                if names is not None and entry.name not in names:
                    continue
                key = entry.name[:-5]
                if not _KEY.fullmatch(key):
                    raise LockInventoryError(f"migration lock has invalid name: {entry.name}")
                value = entry.stat(follow_symlinks=False)
                self._validate(entry.name, value)
                result[entry.name] = _identity(value)
        return dict(sorted(result.items()))

    def _ensure(self, key: str) -> None:
        if not _KEY.fullmatch(key):
            raise LockInventoryError(f"unsafe derived migration lock key: {key}")
        name = f"{key}.lock"
        directory_fd = self._directory()
        try:
            fd = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            return
        except OSError as exc:
            raise LockInventoryError(f"cannot create migration lock {name}: {exc}") from exc
        else:
            os.close(fd)

    def _open_and_lock(
        self,
        name: str,
        expected: tuple[int, int, int, int, int],
    ) -> int:
        try:
            fd = os.open(
                name,
                os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self._directory(),
            )
        except OSError as exc:
            raise LockInventoryError(f"cannot safely open migration lock {name}: {exc}") from exc
        try:
            value = os.fstat(fd)
            self._validate(name, value)
            if _identity(value) != expected:
                raise LockInventoryError(f"migration lock inode changed before acquisition: {name}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockContentionError(f"migration lock is already owned: {name}") from exc
            return fd
        except BaseException:
            os.close(fd)
            raise

    def acquire(
        self, keys: list[str], *, include_existing: bool = False
    ) -> dict[str, object]:
        for key in sorted(set(keys)):
            self._ensure(key)
        names = None if include_existing else {f"{key}.lock" for key in keys}
        before = self._snapshot(names)
        for name, expected in before.items():
            self._lock_fds.append(self._open_and_lock(name, expected))
        after = self._snapshot(names)
        if after != before:
            raise LockInventoryError("migration lock inode set changed during acquisition")
        return {
            "status": "ready",
            "locks": len(before),
            "uid": self.uid,
            "gid": self.gid,
        }

    def close(self) -> None:
        while self._lock_fds:
            fd = self._lock_fds.pop()
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None


def hold(
    root: Path,
    keys: list[str],
    *,
    include_existing: bool = False,
    input_stream: BinaryIO = sys.stdin.buffer,
) -> None:
    lease = LockLease(root)
    try:
        receipt = lease.acquire(keys, include_existing=include_existing)
        print(json.dumps(receipt, sort_keys=True), flush=True)
        input_stream.read(1)
    finally:
        lease.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-existing", action="store_true")
    parser.add_argument("journal_dir", type=Path)
    parser.add_argument("keys", nargs="+")
    args = parser.parse_args()
    try:
        hold(args.journal_dir, args.keys, include_existing=args.all_existing)
    except LockContentionError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(75) from exc
    except LockInventoryError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(76) from exc


if __name__ == "__main__":
    main()
