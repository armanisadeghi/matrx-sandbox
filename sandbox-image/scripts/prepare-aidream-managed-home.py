#!/usr/bin/env python3
"""Create the fixed managed HOME without traversing an attacker symlink."""

from __future__ import annotations

import os
import stat


PARENT = "/run"
NAME = "aidream-managed-home"


def refuse(message: str) -> None:
    raise SystemExit(f"managed aidream home refused: {message}")


def require_root_owned_directory(metadata: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        refuse(f"{label} is not a directory")
    if metadata.st_uid != 0:
        refuse(f"{label} is not root-owned")
    if metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX:
        refuse(f"{label} is writable without sticky protection")


def main() -> None:
    parent_fd = os.open(PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        require_root_owned_directory(os.fstat(parent_fd), PARENT)
        existed = False
        try:
            os.mkdir(NAME, 0o555, dir_fd=parent_fd)
        except FileExistsError:
            existed = True
            existing = os.stat(NAME, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(existing.st_mode):
                refuse(f"{PARENT}/{NAME} is a symlink")
            if not stat.S_ISDIR(existing.st_mode):
                refuse(f"{PARENT}/{NAME} is not a directory")
            if existing.st_uid != 0:
                refuse(f"{PARENT}/{NAME} already belongs to a non-root user")
        # mkdir is atomic. Under a root-owned sticky parent an agent cannot
        # replace the root-owned child between this name check and open.
        child_fd = os.open(NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            final = os.fstat(child_fd)
            named = os.stat(NAME, dir_fd=parent_fd, follow_symlinks=False)
            if (final.st_dev, final.st_ino) != (named.st_dev, named.st_ino):
                refuse(f"{PARENT}/{NAME} changed while opening")
            require_root_owned_directory(final, f"{PARENT}/{NAME}")
            if existed and stat.S_IMODE(final.st_mode) != 0o555:
                refuse(f"{PARENT}/{NAME} existing mode is unsafe")
            if not existed:
                os.fchown(child_fd, 0, 0)
                os.fchmod(child_fd, 0o555)
                final = os.fstat(child_fd)
        finally:
            os.close(child_fd)
        require_root_owned_directory(final, f"{PARENT}/{NAME}")
        if stat.S_IMODE(final.st_mode) != 0o555:
            refuse(f"{PARENT}/{NAME} has unsafe mode")
    finally:
        os.close(parent_fd)


if __name__ == "__main__":
    main()
