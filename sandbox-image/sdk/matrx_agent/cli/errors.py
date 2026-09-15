"""Named errors for the ``mtx`` CLI — a traceback is never an acceptable answer.

Why this exists (2026-09-15 live regression): a fresh EC2 ``bare`` box booted
with ``~/projects``, ``~/.cache``, ``~/.local`` and ``~/.matrx`` owned by root
while the shell ran as uid 1000, because the root-run S3 home restore created
them and nothing chowned them back. ``mtx new python <name>`` answered the agent
with::

    Traceback (most recent call last):
      ...
    PermissionError: [Errno 13] Permission denied: '/home/agent/projects/x'

A traceback names no remedy, so the agent improvised — exactly what the sanctioned
recipe exists to prevent. Every ``mtx`` command now turns a filesystem permission
failure into one named line plus the command that fixes the box.

The image-side fix for the cause is the ownership chokepoint at the end of
``scripts/ensure-layout.sh``; this module is the second half of the law — the
tool tells the truth even when the box is broken.
"""

from __future__ import annotations

import errno
import os
import sys

#: Errnos that mean "the filesystem refused you", not "your input was wrong".
PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


def is_permission_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and (
        isinstance(exc, PermissionError) or exc.errno in PERMISSION_ERRNOS
    )


def permission_message(exc: OSError, *, action: str) -> str:
    """One line that says what was refused, why, and how to fix the box."""
    path = str(getattr(exc, "filename", None) or "the path")
    who, chown_target = _whoami()
    detail = path
    culprit, culprit_owner = _nearest_existing_owner(path)
    if culprit_owner:
        detail = (
            f"{path} (owned by {culprit_owner})"
            if culprit == os.path.abspath(path)
            else f"{path} — its nearest existing parent {culprit} is owned by {culprit_owner}"
        )
    return (
        f"permission denied while {action}: {detail}. This shell runs as {who}, so a "
        f"root-owned directory inside your own home stops every write. Fix this box "
        f"with:  sudo chown -R {chown_target}:{chown_target} $HOME   — then run the "
        f"command again. (A boot step that runs as root created those paths; the image "
        f"repairs them on every boot in ensure-layout.sh, so a box that still needs the "
        f"manual fix is worth reporting.)"
    )


def _whoami() -> tuple[str, str]:
    """(human name for the message, name safe to pass to chown)."""
    try:
        import pwd

        name = pwd.getpwuid(os.geteuid()).pw_name
        return name, name
    except Exception:  # noqa: BLE001 — identity is best effort
        uid = os.geteuid()
        return f"uid {uid}", str(uid)


def _nearest_existing_owner(path: str) -> tuple[str | None, str | None]:
    """(closest existing path, its owner) — the directory actually at fault."""
    probe = os.path.abspath(path)
    for _ in range(64):
        try:
            st = os.lstat(probe)
        except OSError:
            parent = os.path.dirname(probe)
            if parent == probe:
                return None, None
            probe = parent
            continue
        try:
            import grp
            import pwd

            owner = f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
        except Exception:  # noqa: BLE001
            owner = f"uid {st.st_uid}"
        return probe, owner
    return None, None


def report(exc: OSError, *, prefix: str, action: str) -> int:
    print(f"[{prefix}] {permission_message(exc, action=action)}", file=sys.stderr)
    return 1
