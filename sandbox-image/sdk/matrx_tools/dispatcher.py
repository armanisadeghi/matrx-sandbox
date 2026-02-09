"""Tool dispatcher — routes tool_use blocks to the correct handler function."""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from matrx_tools.session import ToolSession
from matrx_tools.tools.execution import tool_bash, tool_bash_output, tool_task_stop
from matrx_tools.tools.file_ops import (
    tool_edit,
    tool_glob,
    tool_grep,
    tool_multi_edit,
    tool_notebook_edit,
    tool_read,
    tool_write,
)
from matrx_tools.tools.meta import tool_todo_write
from matrx_tools.tools.web import tool_web_fetch, tool_web_search
from matrx_tools.types import ToolResult, ToolResultType

logger = logging.getLogger(__name__)

ToolHandler = Callable[..., Coroutine[Any, Any, ToolResult]]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "Read": tool_read,
    "Write": tool_write,
    "Edit": tool_edit,
    "MultiEdit": tool_multi_edit,
    "Glob": tool_glob,
    "Grep": tool_grep,
    "NotebookEdit": tool_notebook_edit,
    "Bash": tool_bash,
    "BashOutput": tool_bash_output,
    "TaskStop": tool_task_stop,
    "WebFetch": tool_web_fetch,
    "WebSearch": tool_web_search,
    "TodoWrite": tool_todo_write,
}


async def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    session: ToolSession,
) -> ToolResult:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"Unknown tool: {tool_name}. Available tools: {', '.join(sorted(TOOL_HANDLERS))}",
        )
    try:
        return await handler(session=session, **tool_input)
    except TypeError as e:
        logger.warning("Invalid parameters for tool %s: %s", tool_name, e)
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"Invalid parameters for {tool_name}: {e}",
        )
    except Exception as e:
        logger.exception("Tool %s failed with unexpected error", tool_name)
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"Tool {tool_name} failed: {type(e).__name__}: {e}",
        )
