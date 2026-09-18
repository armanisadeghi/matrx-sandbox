"""``browse`` — the sandbox's browser command line.

Why it exists (2026-09-18 field report, P2-2): an agent working in a box typed
``browse`` and got ``command not found``. Every image ships a browser
capability — ``matrx_tools.tools.browser``, routed through the canonical AI
Dream Browser Manager — but that surface was reachable only from the tool
dispatcher, never from a shell. An agent whose whole world is ``shell_execute``
could not use it.

**This is a front end, never a second browser.** Every subcommand below calls
exactly the same ``tool_browser_*`` coroutine the dispatcher calls, against the
same ``BrowserManagerClient``. The sandbox still launches no Chromium, holds no
profile, and receives no worker address or fencing token. If this file ever
grows its own navigation code, that is the parallel-layer defect CLAUDE.md
forbids, not a feature.

**Nothing fails silently.** When the box carries no Browser Manager identity,
``BrowserManagerConfig.from_env`` names the missing environment variables and
this CLI prints that refusal and exits non-zero — it never falls back to a
local browser. ``browse --help`` works on any box, configured or not, which is
what the toolchain guard checks.

Session model: one shell invocation is one run. ``browse open <url> --text``
navigates and prints the page in a single process, which is the shape an agent
actually needs; the Browser Manager holds the page between calls, so a later
``browse text`` in a new process still sees it.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path


def _subparser(parent) -> None:
    """Register ``mtx browse`` (and therefore ``browse``) on the mtx parser."""
    p = parent.add_parser(
        "browse",
        help="Drive the sandbox browser (same engine as the Browser* tools)",
    )
    sub = p.add_subparsers(dest="browse_cmd", required=True)

    open_p = sub.add_parser("open", help="Navigate to a URL")
    open_p.add_argument("url")
    open_p.add_argument(
        "--wait-until",
        default="domcontentloaded",
        choices=["load", "domcontentloaded", "networkidle", "commit"],
    )
    open_p.add_argument(
        "--text",
        action="store_true",
        help="Also print the page text once it has loaded",
    )

    text_p = sub.add_parser("text", help="Print the current page's text")
    text_p.add_argument("--selector", default=None, help="Limit to one element")

    click_p = sub.add_parser("click", help="Click an element")
    click_p.add_argument("target", help="Visible text, or --selector for CSS")
    click_p.add_argument(
        "--selector",
        action="store_true",
        help="Treat TARGET as a CSS selector instead of visible text",
    )

    type_p = sub.add_parser("type", help="Type into an input")
    type_p.add_argument("selector", help="CSS selector of the input")
    type_p.add_argument("text")
    type_p.add_argument("--enter", action="store_true", help="Press Enter after typing")
    type_p.add_argument("--clear", action="store_true", help="Clear the field first")

    scroll_p = sub.add_parser("scroll", help="Scroll the page")
    scroll_p.add_argument("direction", nargs="?", default="down", choices=["up", "down"])
    scroll_p.add_argument("--amount", type=int, default=3)

    wait_p = sub.add_parser("wait", help="Wait for text or a selector to appear")
    wait_p.add_argument("target")
    wait_p.add_argument("--selector", action="store_true", help="TARGET is a CSS selector")
    wait_p.add_argument("--timeout-ms", type=int, default=30_000)

    eval_p = sub.add_parser("eval", help="Evaluate JavaScript and print the result")
    eval_p.add_argument("javascript")

    shot_p = sub.add_parser("shot", help="Screenshot the viewport to a PNG file")
    shot_p.add_argument(
        "out",
        nargs="?",
        default="screenshot.png",
        help="Where to write the PNG (default ./screenshot.png)",
    )

    sub.add_parser("back", help="Go back one page")
    sub.add_parser("tabs", help="Show the active page")
    sub.add_parser("close", help="Close the browser run")


async def _dispatch(args) -> "object":
    # Imported lazily: `browse --help` must work on a box with no Browser
    # Manager identity, and these modules pull in httpx + pydantic.
    from matrx_tools.session import ToolSession
    from matrx_tools.tools import browser as B

    session = ToolSession()
    cmd = args.browse_cmd
    try:
        if cmd == "open":
            result = await B.tool_browser_navigate(session, args.url, wait_until=args.wait_until)
            if args.text and result.type.value != "error":
                page = await B.tool_browser_snapshot(session)
                result.output = f"{result.output}\n\n{page.output}"
                if page.type.value == "error":
                    result = page
            return result
        if cmd == "text":
            return await B.tool_browser_snapshot(session, selector=args.selector)
        if cmd == "click":
            if args.selector:
                return await B.tool_browser_click(session, selector=args.target)
            return await B.tool_browser_click(session, text=args.target)
        if cmd == "type":
            return await B.tool_browser_type(
                session, args.text, selector=args.selector,
                press_enter=args.enter, clear_first=args.clear,
            )
        if cmd == "scroll":
            return await B.tool_browser_scroll(session, direction=args.direction, amount=args.amount)
        if cmd == "wait":
            if args.selector:
                return await B.tool_browser_wait_for(
                    session, selector=args.target, timeout=args.timeout_ms
                )
            return await B.tool_browser_wait_for(session, text=args.target, timeout=args.timeout_ms)
        if cmd == "eval":
            return await B.tool_browser_evaluate(session, args.javascript)
        if cmd == "shot":
            return await B.tool_browser_screenshot(session)
        if cmd == "back":
            return await B.tool_browser_back(session)
        if cmd == "tabs":
            return await B.tool_browser_tabs(session)
        if cmd == "close":
            return await B.tool_browser_close(session)
        raise AssertionError(f"unhandled browse subcommand {cmd!r}")
    finally:
        # `close` is an explicit subcommand; every other invocation leaves the
        # Browser Manager run alive so the next `browse` call sees the page.
        if cmd == "close":
            await session.cleanup()


def run(args) -> int:
    try:
        result = asyncio.run(_dispatch(args))
    except Exception as exc:  # noqa: BLE001 — the refusal must reach the agent verbatim
        print(f"[browse] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "browse_cmd", None) == "shot" and result.image is not None:
        out = Path(args.out).expanduser()
        try:
            out.write_bytes(base64.b64decode(result.image.base64_data))
        except OSError as exc:
            print(f"[browse] could not write {out}: {exc}", file=sys.stderr)
            return 1
        print(f"{result.output}\nSaved to {out}")
        return 0

    if result.type.value == "error":
        print(result.output, file=sys.stderr)
        return 1
    print(result.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point, so ``browse`` is a real command, not only ``mtx browse``."""
    parser = argparse.ArgumentParser(
        prog="browse", description="Drive the sandbox browser from a shell"
    )
    holder = parser.add_subparsers(dest="_holder")
    _subparser(holder)
    # `browse open …` must work; the shim passes argv straight through, and the
    # mtx parser expects the leading "browse" word, so add it when it is absent.
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "browse":
        argv = ["browse", *argv]
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
