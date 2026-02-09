"""Execution tools — Bash, BashOutput, TaskStop."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex

from matrx_tools.session import BackgroundShell, ToolSession
from matrx_tools.types import ToolResult, ToolResultType

logger = logging.getLogger(__name__)

MAX_OUTPUT_LENGTH = 30_000
DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000
CWD_SENTINEL = "___MATRX_CWD_SENTINEL_9f8a7b___"


# ── Bash ───────────────────────────────────────────────────────────────────────


async def tool_bash(
    session: ToolSession,
    command: str,
    description: str | None = None,
    timeout: int | None = None,
    run_in_background: bool = False,
) -> ToolResult:
    if not command or not command.strip():
        return ToolResult(type=ToolResultType.ERROR, output="Command must not be empty.")

    timeout_ms = min(timeout or DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS)
    timeout_s = timeout_ms / 1000.0

    if run_in_background:
        return await _bash_background(session, command)

    return await _bash_foreground(session, command, timeout_s)


async def _bash_foreground(session: ToolSession, command: str, timeout_s: float) -> ToolResult:
    wrapped = (
        f"cd {shlex.quote(session.cwd)} && "
        f"{{ {command} ; }}; "
        f"__exit_code=$?; "
        f'echo "{CWD_SENTINEL}"; '
        f"pwd; "
        f"exit $__exit_code"
    )

    proc = await asyncio.create_subprocess_shell(
        wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        executable="/bin/bash",
        env=_shell_env(session),
    )

    timed_out = False
    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            stdout_bytes = b""

    raw_output = stdout_bytes.decode("utf-8", errors="replace")

    output, new_cwd = _parse_cwd_sentinel(raw_output)
    if new_cwd:
        session.cwd = new_cwd

    if timed_out:
        output = _truncate(output)
        output += f"\n\n[Command timed out after {timeout_s:.0f}s]"
        return ToolResult(output=output)

    output = _truncate(output)

    exit_code = proc.returncode or 0
    if exit_code != 0:
        output += f"\n\n[Exit code: {exit_code}]"

    return ToolResult(output=output)


async def _bash_background(session: ToolSession, command: str) -> ToolResult:
    shell_id = session.next_shell_id()

    wrapped = f"cd {shlex.quote(session.cwd)} && {{ {command} ; }}"

    proc = await asyncio.create_subprocess_shell(
        wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        executable="/bin/bash",
        env=_shell_env(session),
    )

    shell = BackgroundShell(shell_id=shell_id, process=proc)
    session.background_shells[shell_id] = shell

    asyncio.create_task(_collect_background_output(shell))

    return ToolResult(
        output=f"Started background command (shell_id: {shell_id}). Use BashOutput to check on it.",
        metadata={"bash_id": shell_id},
    )


async def _collect_background_output(shell: BackgroundShell) -> None:
    assert shell.process.stdout is not None
    try:
        while True:
            line = await shell.process.stdout.readline()
            if not line:
                break
            shell.output_buffer.append(line.decode("utf-8", errors="replace"))
    except Exception as e:
        shell.output_buffer.append(f"\n[Error reading output: {e}]\n")
    finally:
        try:
            shell.return_code = await asyncio.wait_for(shell.process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            shell.return_code = -1
        shell.is_complete = True


def _parse_cwd_sentinel(raw: str) -> tuple[str, str | None]:
    idx = raw.rfind(CWD_SENTINEL)
    if idx == -1:
        return raw, None

    output = raw[:idx].rstrip("\n")
    after = raw[idx + len(CWD_SENTINEL):].strip()
    lines = after.split("\n")
    new_cwd = lines[0].strip() if lines else None
    return output, new_cwd


def _truncate(output: str) -> str:
    if len(output) > MAX_OUTPUT_LENGTH:
        return output[:MAX_OUTPUT_LENGTH] + "\n\n... [output truncated at 30000 characters]"
    return output


def _shell_env(session: ToolSession) -> dict[str, str]:
    import os
    env = dict(os.environ)
    env["HOME"] = "/home/agent"
    env["USER"] = "agent"
    return env


# ── BashOutput ─────────────────────────────────────────────────────────────────


async def tool_bash_output(
    session: ToolSession,
    bash_id: str,
    filter: str | None = None,
) -> ToolResult:
    shell = session.background_shells.get(bash_id)
    if shell is None:
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"No background shell found with id: {bash_id}",
        )

    new_lines = shell.output_buffer[shell.read_offset:]

    if filter:
        try:
            pattern = re.compile(filter)
        except re.error as e:
            return ToolResult(
                type=ToolResultType.ERROR,
                output=f"Invalid filter regex: {e}",
            )
        matched = [line for line in new_lines if pattern.search(line)]
        shell.read_offset = len(shell.output_buffer)
        output_lines = matched
    else:
        shell.read_offset = len(shell.output_buffer)
        output_lines = new_lines

    output = "".join(output_lines)

    status = "completed" if shell.is_complete else "running"
    status_line = f"\n[Shell {bash_id}: {status}"
    if shell.is_complete and shell.return_code is not None:
        status_line += f", exit code: {shell.return_code}"
    status_line += "]"

    return ToolResult(output=output + status_line)


# ── TaskStop ───────────────────────────────────────────────────────────────────


async def tool_task_stop(
    session: ToolSession,
    task_id: str,
) -> ToolResult:
    shell = session.background_shells.get(task_id)
    if shell is None:
        return ToolResult(
            type=ToolResultType.ERROR,
            output=f"No background task found with id: {task_id}",
        )

    if shell.is_complete:
        return ToolResult(output=f"Task {task_id} has already completed (exit code: {shell.return_code}).")

    try:
        shell.process.kill()
        await asyncio.wait_for(shell.process.wait(), timeout=10.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        pass

    shell.is_complete = True
    shell.return_code = -9

    return ToolResult(output=f"Task {task_id} has been stopped.")
