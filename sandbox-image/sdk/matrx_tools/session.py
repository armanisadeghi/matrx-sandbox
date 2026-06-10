"""ToolSession — mutable state shared across tool calls within a single agent session."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from matrx_tools.types import TodoItem

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)


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


class BrowserSession:
    """Manages a persistent headless Playwright browser across tool calls.

    Launched lazily on first browser tool call. The same browser context and
    page persist across calls so navigation state, cookies, and localStorage
    carry over — just like a real browsing session.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pages: dict[str, Page] = {}
        self._active_page_id: str | None = None
        self._page_counter: int = 0
        self._console_messages: list[dict[str, str]] = []

    @property
    def is_running(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def ensure_browser(self) -> Page:
        if not self.is_running:
            await self._launch()
        assert self._active_page_id is not None
        return self._pages[self._active_page_id]

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright

        logger.info("Launching headless Chromium browser")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await self._context.new_page()
        page_id = self._next_page_id()
        self._pages[page_id] = page
        self._active_page_id = page_id
        page.on("console", lambda msg: self._console_messages.append(
            {"type": msg.type, "text": msg.text}
        ))

    def _next_page_id(self) -> str:
        self._page_counter += 1
        return f"page_{self._page_counter}"

    async def new_tab(self) -> tuple[str, Page]:
        if not self.is_running or self._context is None:
            await self.ensure_browser()
        assert self._context is not None
        page = await self._context.new_page()
        page_id = self._next_page_id()
        self._pages[page_id] = page
        self._active_page_id = page_id
        page.on("console", lambda msg: self._console_messages.append(
            {"type": msg.type, "text": msg.text}
        ))
        return page_id, page

    async def switch_tab(self, page_id: str) -> Page | None:
        if page_id in self._pages:
            self._active_page_id = page_id
            return self._pages[page_id]
        return None

    async def close_tab(self, page_id: str) -> bool:
        page = self._pages.pop(page_id, None)
        if page is None:
            return False
        await page.close()
        if self._active_page_id == page_id:
            if self._pages:
                self._active_page_id = next(iter(self._pages))
            else:
                self._active_page_id = None
        return True

    def list_tabs(self) -> list[dict[str, str]]:
        result = []
        for pid, page in self._pages.items():
            result.append({
                "page_id": pid,
                "url": page.url,
                "title": "",
                "active": pid == self._active_page_id,
            })
        return result

    def pop_console_messages(self) -> list[dict[str, str]]:
        msgs = self._console_messages.copy()
        self._console_messages.clear()
        return msgs

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._pages.clear()
        self._active_page_id = None
        self._console_messages.clear()


class ToolSession:
    def __init__(self, working_dir: str | None = None) -> None:
        self.workspace_root: str = _default_workspace_root()
        self.cwd: str = working_dir or os.environ.get("HOT_PATH", "/home/agent")
        self.files_read: set[str] = set()
        self.background_shells: dict[str, BackgroundShell] = {}
        self._shell_counter: int = 0
        self.todos: list[TodoItem] = []
        self.browser: BrowserSession = BrowserSession()

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
        await self.browser.close()
        for shell in self.background_shells.values():
            if not shell.is_complete:
                try:
                    shell.process.kill()
                except ProcessLookupError:
                    pass
