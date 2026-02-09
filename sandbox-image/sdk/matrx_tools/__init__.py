"""matrx_tools — Claude Code-compatible tools for Matrx sandboxes.

Usage::

    from matrx_tools import TOOL_DEFINITIONS, dispatch, ToolSession

    session = ToolSession()

    # Pass TOOL_DEFINITIONS as tools= to the Claude API
    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        tools=TOOL_DEFINITIONS,
        messages=messages,
    )

    # Dispatch tool_use blocks
    for block in response.content:
        if block.type == "tool_use":
            result = await dispatch(block.name, block.input, session)
            # Convert result to tool_result message...
"""

from matrx_tools.dispatcher import dispatch
from matrx_tools.schemas import TOOL_DEFINITIONS, TOOL_NAMES, TOOL_SCHEMA_MAP
from matrx_tools.session import ToolSession
from matrx_tools.types import ImageData, ToolResult, ToolResultType

__version__ = "0.1.0"
__all__ = [
    "TOOL_DEFINITIONS",
    "TOOL_NAMES",
    "TOOL_SCHEMA_MAP",
    "dispatch",
    "ToolSession",
    "ToolResult",
    "ToolResultType",
    "ImageData",
]
