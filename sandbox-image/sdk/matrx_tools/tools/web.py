"""Web tools — WebFetch and WebSearch placeholders.

These will be replaced with real implementations from the main scaffolding
project. For now they log the request and return a placeholder message.
"""

from __future__ import annotations

import logging

from matrx_tools.session import ToolSession
from matrx_tools.types import ToolResult

logger = logging.getLogger(__name__)


async def tool_web_fetch(
    session: ToolSession,
    url: str,
    prompt: str,
) -> ToolResult:
    logger.info("WebFetch placeholder called: url=%s prompt=%s", url, prompt[:100])
    return ToolResult(
        output=(
            f"WebFetch is not yet implemented in the sandbox environment.\n"
            f"URL: {url}\n"
            f"Prompt: {prompt}\n\n"
            f"This tool will be connected to the main scaffolding project's "
            f"web fetch implementation in a future update."
        ),
    )


async def tool_web_search(
    session: ToolSession,
    query: str,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> ToolResult:
    logger.info("WebSearch placeholder called: query=%s", query)
    return ToolResult(
        output=(
            f"WebSearch is not yet implemented in the sandbox environment.\n"
            f"Query: {query}\n"
            f"Allowed domains: {allowed_domains}\n"
            f"Blocked domains: {blocked_domains}\n\n"
            f"This tool will be connected to the main scaffolding project's "
            f"web search implementation in a future update."
        ),
    )
