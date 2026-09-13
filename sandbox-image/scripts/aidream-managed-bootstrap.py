#!/usr/bin/env python3
"""Start the certified aidream source without trusting process import state.

This is deliberately a fixed-path launcher, not a general-purpose Python
wrapper.  The hosted entrypoint has already verified that this source lives on
a read-only mount; this program additionally refuses symlinked path segments
before putting the one allowed project root on ``sys.path``.
"""

from __future__ import annotations

import os
import runpy
import stat
import sys
from pathlib import Path


TEMPLATE_ROOT = Path("/opt/aidream-template")
RUN_FILE = TEMPLATE_ROOT / "run.py"


def _refuse(message: str) -> None:
    raise SystemExit(f"managed aidream bootstrap refused: {message}")


def _require_plain_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _refuse(f"missing trusted path: {path}")
    if stat.S_ISLNK(metadata.st_mode):
        _refuse(f"symlinked trusted path: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        _refuse(f"trusted path is not a directory: {path}")
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        _refuse(f"trusted path is not root-owned and non-writable: {path}")


def _require_plain_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _refuse(f"missing trusted entrypoint: {path}")
    if stat.S_ISLNK(metadata.st_mode):
        _refuse(f"symlinked trusted entrypoint: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        _refuse(f"trusted entrypoint is not a regular file: {path}")
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        _refuse(f"trusted entrypoint is not root-owned and non-writable: {path}")


def _require_read_only_root() -> None:
    if not os.statvfs(TEMPLATE_ROOT).f_flag & getattr(os, "ST_RDONLY", 1):
        _refuse(f"trusted root is not read-only: {TEMPLATE_ROOT}")


def main() -> None:
    # Check each fixed component rather than resolving a caller-supplied path.
    _require_plain_directory(Path("/opt"))
    _require_plain_directory(TEMPLATE_ROOT)
    _require_plain_file(RUN_FILE)
    _require_read_only_root()
    if TEMPLATE_ROOT.resolve(strict=True) != TEMPLATE_ROOT:
        _refuse(f"trusted root resolves elsewhere: {TEMPLATE_ROOT}")

    # ``-I`` removes the project directory from sys.path.  Add back precisely
    # the immutable root, then retain normal run.py __main__ semantics.
    os.chdir(TEMPLATE_ROOT)
    sys.path.insert(0, os.fspath(TEMPLATE_ROOT))
    if sys.argv[1:] == ["--verify-imports"]:
        # Build-time forcing guard: execute the real run.py import chain, but
        # do not bind a server or require deployment credentials in docker
        # build verification.  The production invocation below keeps __main__.
        runpy.run_path(os.fspath(RUN_FILE))
        return
    if len(sys.argv) != 1:
        _refuse("unsupported arguments")
    runpy.run_path(os.fspath(RUN_FILE), run_name="__main__")


if __name__ == "__main__":
    main()
