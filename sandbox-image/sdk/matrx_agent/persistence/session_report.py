"""Render ``/home/agent/.matrx/session-report.md`` from the prior manifest.

Called once on container startup. The report is the user's first-line communication
about what was preserved, what was stashed, and what was lost. Written to
``$HOME/.matrx/session-report.md`` and surfaced by the matrx-frontend editor on
connect.

The report is intentionally Markdown (renderable in the editor) and short (~30 lines).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from matrx_agent.persistence.manifest import HOME, MATRX_DIR, read_prior_manifest

logger = logging.getLogger(__name__)

REPORT_PATH = MATRX_DIR / "session-report.md"


def _bytes_human(n: int | None) -> str:
    if n is None or n < 0:
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}" if f < 10 else f"{f:.0f} {units[i]}"


def render_report(prior_manifest: dict[str, Any] | None = None) -> str:
    """Build (and persist) the session-report.md from the prior manifest.

    If there is no prior manifest, the report says "fresh start" — that's
    the message a brand-new user sees on their first sandbox.
    """
    MATRX_DIR.mkdir(parents=True, exist_ok=True)

    manifest = prior_manifest if prior_manifest is not None else read_prior_manifest()

    if manifest is None:
        report = (
            "# Welcome\n"
            "\n"
            "This looks like your first sandbox session — nothing to restore.\n"
            "\n"
            "Anything you save under `/home/agent/` will follow you to the next session.\n"
            "Things outside `/home/agent/` (root filesystem, environment variables,\n"
            "running processes) are NOT preserved.\n"
        )
        REPORT_PATH.write_text(report)
        return report

    captured_at = manifest.get("captured_at", "unknown")
    graceful = manifest.get("graceful", False)
    repos = manifest.get("repos") or []
    procs = manifest.get("processes_at_shutdown") or []
    ports = manifest.get("ports_at_shutdown") or []
    size = manifest.get("size") or {}
    home_bytes = size.get("home_bytes")
    transient = manifest.get("transient_things_we_could_not_save") or []

    closed = "gracefully" if graceful else "via crash / force-kill (some last-minute work may be lost)"

    lines = [
        "# Welcome back",
        "",
        f"Restored from your last session — closed {captured_at} {closed}.",
        "",
        "## ✅ What was restored",
        "",
        f"- {_bytes_human(home_bytes)} of files in `/home/agent/`",
    ]

    if repos:
        lines.append(f"- {len(repos)} git repositor{'y' if len(repos) == 1 else 'ies'}:")
        for r in repos:
            path = r.get("path", "?")
            branch = r.get("branch") or "(detached)"
            clean_marker = "" if r.get("clean", True) else " · had uncommitted changes (see below)"
            lines.append(f"  - `{path}` on `{branch}`{clean_marker}")

        # Auto-stash callouts
        stash_blurbs: list[str] = []
        for r in repos:
            auto = r.get("auto_stash")
            if not auto:
                continue
            path = r.get("path", "?")
            uncommitted = r.get("uncommitted_files") or []
            n = len(uncommitted)
            line = f"  - **{path}** — {n} uncommitted file{'s' if n != 1 else ''} were auto-stashed."
            if auto.get("pushed_to_remote") and auto.get("pushed_branch"):
                line += f" Pushed to `{auto['pushed_branch']}` on origin."
            elif auto.get("error"):
                line += f" ⚠️ Stash error: {auto['error']}"
            else:
                line += " Local stash only (no remote credentials configured)."
            stash_blurbs.append(line)

        if stash_blurbs:
            lines += ["", "### Auto-stashed work", ""]
            lines += stash_blurbs
            lines += [
                "",
                "Apply with `git stash pop` inside the repo, or open the Source Control panel "
                "in the editor.",
            ]

    if manifest.get("shell_history_tail"):
        lines += [
            f"- Shell history (last {len(manifest['shell_history_tail'])} commands)",
        ]

    lines += [
        "",
        "## ❌ What was NOT restored",
        "",
    ]

    if procs:
        proc_list = ", ".join(
            f"`{p.get('command', '?')[:40]}` (pid {p.get('pid', '?')})"
            for p in procs[:5]
        )
        lines.append(f"- {len(procs)} process{'es' if len(procs) != 1 else ''} that were running: {proc_list}")
        if ports:
            lines.append(f"  - Listening ports: {', '.join(str(p) for p in ports)}")
        lines.append("  - Restart manually if you need any of them.")

    for note in transient:
        if any(note.startswith(p) for p in ("running processes", "open editor tabs")):
            continue  # already covered above or not relevant here
        lines.append(f"- {note}")

    lines += [
        "",
        "## 💡 Quick reference",
        "",
        "- This report lives at `~/.matrx/session-report.md`.",
        "- Detailed snapshot: `~/.matrx/session.json`.",
        "- Your home directory is mounted from a per-user persistent volume — anything you save here follows you.",
        "",
    ]

    report = "\n".join(lines)
    REPORT_PATH.write_text(report)
    return report
