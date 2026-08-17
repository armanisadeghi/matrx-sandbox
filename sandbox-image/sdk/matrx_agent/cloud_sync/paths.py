from __future__ import annotations

SYSTEM_PATH_ROOTS = frozenset({"generations", "system-files"})


def is_system_path(path: str) -> bool:
    if not path:
        return False
    return path.lstrip("/").split("/", 1)[0] in SYSTEM_PATH_ROOTS
