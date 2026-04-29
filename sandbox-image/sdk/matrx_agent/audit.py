"""Local audit logger for matrx_agent service actions.

Mirrors the on-disk format used by ``matrx_ai.tools._call_logger`` (in the
aidream repo) so the agent's tool-call audit trail under
``~/.matrx/runtime/tool-calls/`` looks the same regardless of who wrote
the entry — agent-driven shell tools (matrx-ai writes via the orchestrator
proxy) or service-initiated actions (matrx_agent writes locally).

Why duplicate the format here instead of importing it
-----------------------------------------------------

matrx-ai is part of the aidream monorepo and is not installed inside the
sandbox container. matrx_agent (this package) lives inside the container.
A shared library would mean a third repo or an awkward optional install.
Both surfaces produce the same markdown shape, so duplicating the writer
is cheaper than the cross-repo dependency.

If the format ever changes, update both:
  - ``aidream/packages/matrx-ai/matrx_ai/tools/_call_logger.py``
  - ``sandbox-image/sdk/matrx_agent/audit.py``  (this file)

Frontmatter convention
----------------------
::

    ---
    tool: cloud_sync_put
    call_id: ""
    message_id: ""
    conversation_id: ""
    user_id: <USER_ID env>
    sandbox_id: <SANDBOX_ID env>
    timestamp: 2026-04-29T18:42:13.123Z
    unix_ts: 1730000000
    duration_ms: 187
    success: true
    inputs:
      path: "notes/system-prompt-feedback.md"
      bytes: 14523
    output_summary:
      path: "notes/system-prompt-feedback.md"
      bytes: 14523
      version: 4
    ---

    ## result
    ```json
    {...}
    ```

Service actions use ``conversation_id = "_runtime"`` so they land under
``~/.matrx/runtime/tool-calls/_runtime/`` and don't pollute the per-
conversation history (the agent can still see them when scanning the
parent directory).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_AGENT_HOME = os.environ.get("AGENT_HOME", "/home/agent")
_TOOL_CALL_SUBDIR = ".matrx/runtime/tool-calls"
_DEFAULT_BUCKET = "_runtime"  # where service actions go


def _safe_segment(value: str | None, default: str) -> str:
    if not value:
        return default
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "_", value)
    return cleaned[:64] or default


def _short_call_id(call_id: str) -> str:
    if not call_id:
        return "anonymous"
    cleaned = re.sub(r"^(toolu_|call_)", "", call_id)
    return cleaned[-8:] if len(cleaned) > 8 else cleaned


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        if "\n" in value or len(value) > 80 or any(c in value for c in ":#\"'"):
            indented = "\n".join("    " + line for line in value.splitlines())
            return f"|\n{indented}"
        return json.dumps(value)
    return json.dumps(value)


def _format_inputs(inputs: dict[str, Any]) -> str:
    if not inputs:
        return "  {}"
    lines: list[str] = []
    for k, v in inputs.items():
        rendered = _yaml_scalar(v)
        lines.append(f"  {k}: {rendered}")
    return "\n".join(lines)


def _build_body(
    *,
    tool: str,
    call_id: str,
    conversation_id: str,
    user_id: str,
    sandbox_id: str,
    duration_ms: int,
    success: bool,
    inputs: dict[str, Any],
    result: dict[str, Any] | None,
    error: str | None,
) -> str:
    now = datetime.now(timezone.utc)
    iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    unix_ts = int(now.timestamp())

    extra: dict[str, Any] = {}
    if isinstance(result, dict):
        for k in ("path", "bytes", "version", "status"):
            if k in result:
                extra[k] = result[k]

    lines = [
        "---",
        f"tool: {tool}",
        f"call_id: {json.dumps(call_id or '')}",
        f"message_id: {json.dumps('')}",
        f"conversation_id: {json.dumps(conversation_id or '')}",
        f"user_id: {json.dumps(user_id or '')}",
        f"sandbox_id: {json.dumps(sandbox_id or '')}",
        f"timestamp: {iso}",
        f"unix_ts: {unix_ts}",
        f"duration_ms: {duration_ms}",
        f"success: {'true' if success else 'false'}",
        "inputs:",
        _format_inputs(inputs),
    ]
    if extra:
        lines.append("output_summary:")
        for k, v in extra.items():
            lines.append(f"  {k}: {_yaml_scalar(v)}")
    lines.append("---")

    parts = ["\n".join(lines), ""]
    if result is not None:
        parts.append("## result\n")
        parts.append("```json")
        parts.append(json.dumps(result, indent=2, default=str))
        parts.append("```\n")
    if error:
        parts.append("## error\n")
        parts.append("```")
        parts.append(error)
        parts.append("```\n")
    return "\n".join(parts)


def _resolve_log_path(
    *,
    conversation_id: str | None,
    tool: str,
    call_id: str,
    unix_ts: int,
) -> Path:
    conv_segment = _safe_segment(conversation_id, _DEFAULT_BUCKET)
    tool_segment = _safe_segment(tool, "tool")
    short_id = _short_call_id(call_id)
    filename = f"{unix_ts}-{tool_segment}-{short_id}.md"
    return Path(_AGENT_HOME) / _TOOL_CALL_SUBDIR / conv_segment / filename


async def write_action_log(
    *,
    tool: str,
    inputs: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    success: bool = True,
    error: str | None = None,
    duration_ms: int = 0,
    call_id: str = "",
    conversation_id: str | None = None,
) -> str | None:
    """Write an audit record to ``~/.matrx/runtime/tool-calls/<bucket>/``.

    Defaults the bucket to ``_runtime`` for service-initiated actions
    (cloud_sync watcher, etc.) so they don't mix with per-conversation
    tool-call history. Pass an explicit ``conversation_id`` to scope an
    action to a specific chat (rare for in-sandbox services).

    Best-effort. Never raises. Returns the agent-facing path on success
    (``~/.matrx/...``) or ``None`` on failure.
    """
    user_id = os.environ.get("USER_ID", "")
    sandbox_id = os.environ.get("SANDBOX_ID", "")

    unix_ts = int(asyncio.get_event_loop().time() if False else __import__("time").time())
    abs_path = _resolve_log_path(
        conversation_id=conversation_id or _DEFAULT_BUCKET,
        tool=tool,
        call_id=call_id,
        unix_ts=unix_ts,
    )

    body = _build_body(
        tool=tool,
        call_id=call_id,
        conversation_id=conversation_id or _DEFAULT_BUCKET,
        user_id=user_id,
        sandbox_id=sandbox_id,
        duration_ms=duration_ms,
        success=success,
        inputs=inputs or {},
        result=result,
        error=error,
    )

    try:
        # write_text is sync; offload so we don't block the event loop on
        # slow disks. Best-effort: never raises if the dir/file is bad.
        await asyncio.to_thread(_write_atomic, abs_path, body)
    except Exception as exc:
        logger.warning("audit: log write failed for %s: %s", tool, exc)
        return None

    # Agent-facing path with ~/ for readability in returned metadata.
    rel = str(abs_path).replace(_AGENT_HOME, "~", 1)
    return rel


def _write_atomic(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


__all__ = ["write_action_log"]
