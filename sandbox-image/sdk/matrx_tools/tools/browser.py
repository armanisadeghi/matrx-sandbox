"""Browser tools — Playwright-backed headless Chromium automation.

All tools operate on a persistent browser session managed by ToolSession.browser.
The browser is launched lazily on first use and persists across calls so navigation
state, cookies, and localStorage carry over.
"""

from __future__ import annotations

import base64
import logging
import re

from matrx_tools.session import ToolSession
from matrx_tools.types import ImageData, ToolResult, ToolResultType

logger = logging.getLogger(__name__)

SNAPSHOT_MAX_LENGTH = 50_000
DEFAULT_TIMEOUT_MS = 30_000


# ── BrowserNavigate ────────────────────────────────────────────────────────────


async def tool_browser_navigate(
    session: ToolSession,
    url: str,
    wait_until: str = "domcontentloaded",
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()
        response = await page.goto(url, wait_until=wait_until, timeout=DEFAULT_TIMEOUT_MS)
        status = response.status if response else "unknown"
        title = await page.title()
        return ToolResult(
            output=f"Navigated to {page.url}\nStatus: {status}\nTitle: {title}",
        )
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Navigation failed: {e}")


# ── BrowserSnapshot ────────────────────────────────────────────────────────────


async def tool_browser_snapshot(
    session: ToolSession,
    selector: str | None = None,
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()
        title = await page.title()

        if selector:
            element = await page.query_selector(selector)
            if not element:
                return ToolResult(
                    type=ToolResultType.ERROR,
                    output=f"Element not found: {selector}",
                )
            snapshot = await element.evaluate("el => el.outerHTML")
        else:
            snapshot = await page.evaluate("""() => {
                function getAccessibilityTree(element, depth = 0, maxDepth = 8) {
                    if (depth > maxDepth) return '';
                    const indent = '  '.repeat(depth);
                    let result = '';
                    const role = element.getAttribute('role') ||
                                 element.tagName.toLowerCase();
                    const name = element.getAttribute('aria-label') ||
                                 element.getAttribute('alt') ||
                                 element.getAttribute('title') ||
                                 element.getAttribute('placeholder') || '';
                    const text = element.childNodes.length === 1 &&
                                 element.childNodes[0].nodeType === 3
                                 ? element.childNodes[0].textContent.trim() : '';
                    const value = element.value || '';
                    const href = element.getAttribute('href') || '';
                    const type = element.getAttribute('type') || '';

                    let label = role;
                    if (name) label += ` "${name}"`;
                    if (text && text.length < 200) label += ` "${text}"`;
                    if (value) label += ` value="${value}"`;
                    if (href) label += ` href="${href}"`;
                    if (type) label += ` type="${type}"`;
                    if (element.disabled) label += ' [disabled]';
                    if (element.getAttribute('aria-expanded'))
                        label += ` expanded=${element.getAttribute('aria-expanded')}`;

                    const skip = ['script', 'style', 'noscript', 'svg', 'path'];
                    if (skip.includes(element.tagName.toLowerCase())) return '';

                    result += indent + label + '\\n';

                    for (const child of element.children) {
                        result += getAccessibilityTree(child, depth + 1, maxDepth);
                    }
                    return result;
                }
                return getAccessibilityTree(document.body);
            }""")

        if len(snapshot) > SNAPSHOT_MAX_LENGTH:
            snapshot = snapshot[:SNAPSHOT_MAX_LENGTH] + "\n... [truncated]"

        return ToolResult(
            output=f"Page: {page.url}\nTitle: {title}\n\n{snapshot}",
        )
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Snapshot failed: {e}")


# ── BrowserScreenshot ──────────────────────────────────────────────────────────


async def tool_browser_screenshot(
    session: ToolSession,
    full_page: bool = False,
    selector: str | None = None,
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()

        if selector:
            element = await page.query_selector(selector)
            if not element:
                return ToolResult(
                    type=ToolResultType.ERROR,
                    output=f"Element not found: {selector}",
                )
            raw = await element.screenshot(type="png")
        else:
            raw = await page.screenshot(type="png", full_page=full_page)

        encoded = base64.b64encode(raw).decode("ascii")
        size_kb = len(raw) / 1024
        title = await page.title()

        return ToolResult(
            output=f"Screenshot of {page.url} ({size_kb:.1f} KB)\nTitle: {title}",
            image=ImageData(media_type="image/png", base64_data=encoded),
        )
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Screenshot failed: {e}")


# ── BrowserClick ───────────────────────────────────────────────────────────────


async def tool_browser_click(
    session: ToolSession,
    selector: str | None = None,
    text: str | None = None,
    position: dict | None = None,
    button: str = "left",
    click_count: int = 1,
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()

        if text:
            locator = page.get_by_text(text, exact=False).first
            await locator.click(button=button, click_count=click_count, timeout=DEFAULT_TIMEOUT_MS)
            return ToolResult(output=f"Clicked text: '{text}'")
        elif selector:
            kwargs: dict = {"button": button, "click_count": click_count, "timeout": DEFAULT_TIMEOUT_MS}
            if position:
                kwargs["position"] = position
            await page.click(selector, **kwargs)
            return ToolResult(output=f"Clicked: {selector}")
        elif position:
            await page.mouse.click(position.get("x", 0), position.get("y", 0), button=button, click_count=click_count)
            return ToolResult(output=f"Clicked at position ({position.get('x')}, {position.get('y')})")
        else:
            return ToolResult(
                type=ToolResultType.ERROR,
                output="Provide 'selector', 'text', or 'position' to identify what to click.",
            )
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Click failed: {e}")


# ── BrowserType ────────────────────────────────────────────────────────────────


async def tool_browser_type(
    session: ToolSession,
    text: str,
    selector: str | None = None,
    press_enter: bool = False,
    clear_first: bool = False,
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()

        if selector:
            if clear_first:
                await page.fill(selector, "", timeout=DEFAULT_TIMEOUT_MS)
            await page.type(selector, text, timeout=DEFAULT_TIMEOUT_MS)
            desc = f"Typed into {selector}"
        else:
            await page.keyboard.type(text)
            desc = "Typed text (no selector — typed to focused element)"

        if press_enter:
            await page.keyboard.press("Enter")
            desc += " + pressed Enter"

        return ToolResult(output=desc)
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Type failed: {e}")


# ── BrowserPressKey ────────────────────────────────────────────────────────────


async def tool_browser_press_key(
    session: ToolSession,
    key: str,
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()
        await page.keyboard.press(key)
        return ToolResult(output=f"Pressed key: {key}")
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Key press failed: {e}")


# ── BrowserScroll ──────────────────────────────────────────────────────────────


async def tool_browser_scroll(
    session: ToolSession,
    direction: str = "down",
    amount: int = 3,
    selector: str | None = None,
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()

        delta_map = {
            "down": (0, amount * 100),
            "up": (0, -(amount * 100)),
            "right": (amount * 100, 0),
            "left": (-(amount * 100), 0),
        }
        dx, dy = delta_map.get(direction, (0, amount * 100))

        if selector:
            element = await page.query_selector(selector)
            if element:
                await element.scroll_into_view_if_needed()
                box = await element.bounding_box()
                if box:
                    await page.mouse.wheel(dx, dy)
        else:
            await page.mouse.wheel(dx, dy)

        return ToolResult(output=f"Scrolled {direction} by {amount} units")
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Scroll failed: {e}")


# ── BrowserEvaluate ────────────────────────────────────────────────────────────


async def tool_browser_evaluate(
    session: ToolSession,
    javascript: str,
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()
        result = await page.evaluate(javascript)
        output = str(result) if result is not None else "(no return value)"
        if len(output) > SNAPSHOT_MAX_LENGTH:
            output = output[:SNAPSHOT_MAX_LENGTH] + "\n... [truncated]"
        return ToolResult(output=output)
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"JavaScript evaluation failed: {e}")


# ── BrowserWaitFor ─────────────────────────────────────────────────────────────


async def tool_browser_wait_for(
    session: ToolSession,
    text: str | None = None,
    selector: str | None = None,
    timeout: int = 30000,
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()

        if text:
            await page.wait_for_selector(f"text={text}", timeout=timeout)
            return ToolResult(output=f"Text appeared: '{text}'")
        elif selector:
            await page.wait_for_selector(selector, timeout=timeout)
            return ToolResult(output=f"Element appeared: {selector}")
        else:
            return ToolResult(
                type=ToolResultType.ERROR,
                output="Provide 'text' or 'selector' to wait for.",
            )
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Wait failed: {e}")


# ── BrowserBack ────────────────────────────────────────────────────────────────


async def tool_browser_back(
    session: ToolSession,
) -> ToolResult:
    try:
        page = await session.browser.ensure_browser()
        await page.go_back(timeout=DEFAULT_TIMEOUT_MS)
        title = await page.title()
        return ToolResult(output=f"Navigated back to {page.url}\nTitle: {title}")
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Back navigation failed: {e}")


# ── BrowserTabs ────────────────────────────────────────────────────────────────


async def tool_browser_tabs(
    session: ToolSession,
    action: str = "list",
    page_id: str | None = None,
    url: str | None = None,
) -> ToolResult:
    try:
        if action == "list":
            tabs = session.browser.list_tabs()
            if not tabs:
                return ToolResult(output="No browser tabs open.")
            lines = []
            for t in tabs:
                marker = " (active)" if t["active"] else ""
                lines.append(f"  {t['page_id']}: {t['url']}{marker}")
            return ToolResult(output=f"Open tabs ({len(tabs)}):\n" + "\n".join(lines))

        elif action == "new":
            pid, page = await session.browser.new_tab()
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
            return ToolResult(output=f"Created new tab {pid}" + (f" at {url}" if url else ""))

        elif action == "switch":
            if not page_id:
                return ToolResult(type=ToolResultType.ERROR, output="page_id required for 'switch' action")
            page = await session.browser.switch_tab(page_id)
            if page is None:
                return ToolResult(type=ToolResultType.ERROR, output=f"Tab not found: {page_id}")
            return ToolResult(output=f"Switched to tab {page_id}: {page.url}")

        elif action == "close":
            if not page_id:
                return ToolResult(type=ToolResultType.ERROR, output="page_id required for 'close' action")
            closed = await session.browser.close_tab(page_id)
            if not closed:
                return ToolResult(type=ToolResultType.ERROR, output=f"Tab not found: {page_id}")
            return ToolResult(output=f"Closed tab {page_id}")

        else:
            return ToolResult(
                type=ToolResultType.ERROR,
                output=f"Unknown action: {action}. Use 'list', 'new', 'switch', or 'close'.",
            )
    except Exception as e:
        return ToolResult(type=ToolResultType.ERROR, output=f"Tab operation failed: {e}")


# ── BrowserConsole ─────────────────────────────────────────────────────────────


async def tool_browser_console(
    session: ToolSession,
    pattern: str | None = None,
) -> ToolResult:
    messages = session.browser.pop_console_messages()
    if pattern:
        try:
            regex = re.compile(pattern)
            messages = [m for m in messages if regex.search(m.get("text", ""))]
        except re.error as e:
            return ToolResult(type=ToolResultType.ERROR, output=f"Invalid pattern: {e}")

    if not messages:
        return ToolResult(output="No console messages.")

    lines = [f"[{m.get('type', '?')}] {m.get('text', '')}" for m in messages]
    return ToolResult(output=f"Console messages ({len(lines)}):\n" + "\n".join(lines))


# ── BrowserClose ───────────────────────────────────────────────────────────────


async def tool_browser_close(
    session: ToolSession,
) -> ToolResult:
    if not session.browser.is_running:
        return ToolResult(output="Browser is not running.")

    await session.browser.close()
    return ToolResult(output="Browser closed.")
