"""Browser tools routed exclusively through the canonical Browser Manager."""

from __future__ import annotations

import json
from typing import Any

from matrx_tools.session import ToolSession
from matrx_tools.types import ImageData, ToolResult, ToolResultType


def _error(message: str) -> ToolResult:
    return ToolResult(type=ToolResultType.ERROR, output=message)


def _result_payload(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    return result if isinstance(result, dict) else {}


async def _command(session: ToolSession, command: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    response = await session.browser_client().command(command)
    return response, _result_payload(response)


async def tool_browser_navigate(
    session: ToolSession, url: str, wait_until: str = "domcontentloaded"
) -> ToolResult:
    try:
        response, result = await _command(
            session,
            {
                "command": "navigate", "url": url, "wait_until": wait_until,
                "timeout_ms": 30_000, "extract_text": False,
            },
        )
        return ToolResult(
            output=(f"Navigated to {result.get('url') or url}\n"
                    f"Status: {result.get('http_status', 'unknown')}\n"
                    f"Title: {result.get('title') or ''}"),
            metadata={"run_id": response.get("run_id"), "page_id": response.get("active_page_id")},
        )
    except Exception as exc:
        return _error(f"Navigation failed: {exc}")


async def tool_browser_snapshot(session: ToolSession, selector: str | None = None) -> ToolResult:
    try:
        if selector:
            response, result = await _command(
                session, {"command": "get_element", "selector": selector, "include_html": True}
            )
            if not result.get("found"):
                return _error(f"Element not found: {selector}")
            content = result.get("outer_html") or result.get("text") or ""
        else:
            response, result = await _command(
                session, {"command": "get_text", "selector": "body", "cap": 50_000}
            )
            content = result.get("text") or ""
        url = result.get("url") or session.browser_client().current_url or ""
        return ToolResult(
            output=f"Page: {url}\n\n{content}",
            metadata={"run_id": response.get("run_id"), "page_id": response.get("active_page_id")},
        )
    except Exception as exc:
        return _error(f"Snapshot failed: {exc}")


async def tool_browser_screenshot(
    session: ToolSession, full_page: bool = False, selector: str | None = None
) -> ToolResult:
    if full_page or selector:
        return _error("The canonical sandbox screenshot currently captures the active viewport only.")
    try:
        response = await session.browser_client().capture()
        artifact = response.get("artifact")
        if not isinstance(artifact, dict) or not artifact.get("image_base64"):
            return _error("Screenshot failed: Browser Manager returned no image.")
        size_kb = float(artifact.get("byte_count") or 0) / 1024
        return ToolResult(
            output=f"Screenshot of {session.browser_client().current_url or ''} ({size_kb:.1f} KB)",
            image=ImageData(
                media_type=str(artifact.get("media_type") or "image/png"),
                base64_data=str(artifact["image_base64"]),
            ),
            metadata={"run_id": response.get("run_id")},
        )
    except Exception as exc:
        return _error(f"Screenshot failed: {exc}")


async def tool_browser_click(
    session: ToolSession, selector: str | None = None, text: str | None = None,
    position: dict | None = None, button: str = "left", click_count: int = 1,
) -> ToolResult:
    if position is not None or button != "left" or click_count != 1:
        return _error("Position, alternate-button, and multi-click actions are not in the canonical worker contract.")
    target = selector or (f"text={text}" if text else None)
    if not target:
        return _error("Provide 'selector' or 'text' to identify what to click.")
    try:
        await _command(session, {
            "command": "click", "selector": target, "wait_after_ms": 0, "timeout_ms": 10_000,
        })
        return ToolResult(output=f"Clicked: {target}")
    except Exception as exc:
        return _error(f"Click failed: {exc}")


async def tool_browser_type(
    session: ToolSession, text: str, selector: str | None = None,
    press_enter: bool = False, clear_first: bool = False,
) -> ToolResult:
    if not selector:
        return _error("A selector is required by the canonical browser worker for typed text.")
    try:
        await _command(session, {
            "command": "type_text", "selector": selector, "text": text,
            "clear_first": clear_first, "press_enter": press_enter, "timeout_ms": 10_000,
        })
        return ToolResult(output=f"Typed into {selector}" + (" + pressed Enter" if press_enter else ""))
    except Exception as exc:
        return _error(f"Type failed: {exc}")


async def tool_browser_press_key(session: ToolSession, key: str) -> ToolResult:
    del session, key
    return _error("Free-form key presses are not in the canonical browser worker contract.")


async def tool_browser_scroll(
    session: ToolSession, direction: str = "down", amount: int = 3,
    selector: str | None = None,
) -> ToolResult:
    if direction not in {"up", "down"}:
        return _error("The canonical browser worker supports vertical scrolling only.")
    try:
        await _command(session, {
            "command": "scroll", "direction": direction, "pixels": amount * 100,
            "selector": selector,
        })
        return ToolResult(output=f"Scrolled {direction} by {amount} units")
    except Exception as exc:
        return _error(f"Scroll failed: {exc}")


async def tool_browser_evaluate(session: ToolSession, javascript: str) -> ToolResult:
    try:
        _, result = await _command(session, {"command": "eval_js", "expression": javascript})
        return ToolResult(output=json.dumps(result.get("value"), ensure_ascii=False, default=str))
    except Exception as exc:
        return _error(f"JavaScript evaluation failed: {exc}")


async def tool_browser_wait_for(
    session: ToolSession, text: str | None = None, selector: str | None = None,
    timeout: int = 30_000,
) -> ToolResult:
    if not text and not selector:
        return _error("Provide 'text' or 'selector' to wait for.")
    try:
        await _command(session, {
            "command": "wait_for", "selector": selector, "text": text,
            "state": "visible", "timeout_ms": timeout,
        })
        return ToolResult(output=f"Wait completed for: {selector or text}")
    except Exception as exc:
        return _error(f"Wait failed: {exc}")


async def tool_browser_back(session: ToolSession) -> ToolResult:
    try:
        await _command(session, {"command": "eval_js", "expression": "history.back()"})
        return ToolResult(output="Requested browser back navigation.")
    except Exception as exc:
        return _error(f"Back navigation failed: {exc}")


async def tool_browser_tabs(
    session: ToolSession, action: str = "list", page_id: str | None = None,
    url: str | None = None,
) -> ToolResult:
    del page_id, url
    if action != "list":
        return _error("Tab mutation is not yet exposed by the canonical Browser Manager bridge.")
    try:
        client = session.browser_client()
        await client.ensure_run()
        if client.active_page_id is None:
            return ToolResult(output="No active browser page has been observed yet.")
        return ToolResult(
            output=f"Open tabs (known active page):\n  {client.active_page_id}: {client.current_url or ''} (active)",
            metadata={"run_id": client.run_id},
        )
    except Exception as exc:
        return _error(f"Tab operation failed: {exc}")


async def tool_browser_console(session: ToolSession, pattern: str | None = None) -> ToolResult:
    del session, pattern
    return _error("Console collection is not exposed by the canonical Browser Manager contract.")


async def tool_browser_close(session: ToolSession) -> ToolResult:
    client = session.browser_client()
    if not client.is_running:
        return ToolResult(output="Browser is not running.")
    try:
        await client.close()
        return ToolResult(output="Browser closed.")
    except Exception as exc:
        return _error(f"Browser close failed: {exc}")
