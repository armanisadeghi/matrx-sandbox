"""ToolSession — mutable state shared across tool calls within a single agent session."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from matrx_tools.types import TodoItem


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
        self.cwd: str = working_dir or os.environ.get("HOT_PATH", "/home/agent")
        self.files_read: set[str] = set()
        self.background_shells: dict[str, BackgroundShell] = {}
        self._shell_counter: int = 0
        self.todos: list[TodoItem] = []

    def mark_file_read(self, path: str) -> None:
        self.files_read.add(os.path.realpath(path))

    def has_read_file(self, path: str) -> bool:
        return os.path.realpath(path) in self.files_read

    def next_shell_id(self) -> str:
        self._shell_counter += 1
        return f"shell_{self._shell_counter}"

    def resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.join(self.cwd, path)
