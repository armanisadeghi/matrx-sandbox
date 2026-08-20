"""ToolSession — mutable state shared across tool calls within a single agent session."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from matrx_tools.browser_manager import BrowserManagerClient
from matrx_tools.types import TodoItem

class PathEscapesWorkspaceError(ValueError):
    """Raised when a tool path resolves outside the session workspace root.

    The dispatcher turns this into a clean error ToolResult, so callers don't
    need to catch it explicitly.
    """


def _default_workspace_root() -> str:
    """The directory tool file paths are confined to.

    Priority: TOOL_WORKSPACE_BASE / MATRX_TOOLS_WORKSPACE_BASE (the orchestrator
    sets the former for sandbox templates) > HOT_PATH > /home/agent. Realpath'd
    so symlink comparisons in ``resolve_path`` are exact.
    """
    root = (
        os.environ.get("TOOL_WORKSPACE_BASE")
        or os.environ.get("MATRX_TOOLS_WORKSPACE_BASE")
        or os.environ.get("HOT_PATH")
        or "/home/agent"
    )
    return os.path.realpath(root)


@dataclass
class BackgroundShell:
    shell_id: str
    process: asyncio.subprocess.Process
    output_buffer: list[str] = field(default_factory=list)
    read_offset: int = 0
    is_complete: bool = False
    return_code: int | None = None


class ToolSession:
    def __init__(self, working_dir: str | None = None) -> None:
        self.workspace_root: str = _default_workspace_root()
        self.cwd: str = working_dir or os.environ.get("HOT_PATH", "/home/agent")
        self.files_read: set[str] = set()
        self.background_shells: dict[str, BackgroundShell] = {}
        self._shell_counter: int = 0
        self.todos: list[TodoItem] = []
        self.browser: BrowserManagerClient | None = None

    def browser_client(self) -> BrowserManagerClient:
        if self.browser is None:
            self.browser = BrowserManagerClient()
        return self.browser

    def mark_file_read(self, path: str) -> None:
        self.files_read.add(os.path.realpath(path))

    def has_read_file(self, path: str) -> bool:
        return os.path.realpath(path) in self.files_read

    def next_shell_id(self) -> str:
        self._shell_counter += 1
        return f"shell_{self._shell_counter}"

    def resolve_path(self, path: str) -> str:
        """Resolve a tool-supplied path and confine it to the workspace root.

        Absolute paths are honored only if they land inside the workspace;
        relative paths resolve against ``cwd``. Symlinks and ``..`` are resolved
        against the real filesystem (``os.path.realpath`` canonicalizes the
        existing prefix even for a not-yet-created leaf), so a symlinked parent
        cannot be used to escape. Anything resolving outside the root raises
        ``PathEscapesWorkspaceError`` — without this, every file tool could read
        or write arbitrary host paths (``/etc/shadow``, another user's data).
        """
        candidate = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        real = os.path.realpath(candidate)
        root = self.workspace_root
        if real != root and not real.startswith(root + os.sep):
            raise PathEscapesWorkspaceError(
                f"Path escapes the workspace ({root}): {path}"
            )
        return real

    async def cleanup(self) -> None:
        if self.browser is not None:
            await self.browser.close()
        for shell in self.background_shells.values():
            if not shell.is_complete:
                try:
                    shell.process.kill()
                except ProcessLookupError:
                    pass
