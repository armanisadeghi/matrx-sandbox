"""Workspace root the matrx_agent daemon operates against.

Defaults to the sandbox home (`/home/agent`). Set `MATRX_WORKSPACE_ROOT` to run
the SAME daemon against a real host or container root (e.g. `/`, `/srv`, `/root`)
for non-sandbox / control-plane use — the rest of the daemon (fs/exec/git/pty)
already honors caller-supplied absolute paths, so this only affects the few
places that default to the home dir (credential storage, git-clone default cwd,
git-repo discovery).
"""

import os
from pathlib import Path

WORKSPACE_ROOT = Path(
    os.environ.get("MATRX_WORKSPACE_ROOT")
    or os.environ.get("HOT_PATH")
    or "/home/agent"
)
