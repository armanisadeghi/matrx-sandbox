"""A long stdin exec (shell_python) answers honestly instead of a 500.

Before 2026-10-22 the stdin path read the exec socket with the Docker client's
inherited 60 s timeout: a script that printed nothing for a minute raised
``TimeoutError`` out of ``_exec_with_stdin``, the /exec route had no handler for
it, and the agent read "Sandbox call failed (500)" while the script kept running
inside the box (conversation 9b224bf5, 2026-10-22).
"""

from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest


def _frame(stream_id: int, payload: bytes) -> bytes:
    return struct.pack(">BxxxL", stream_id, len(payload)) + payload


class _SilentAfterSocket:
    """Delivers some output, then goes silent: recv raises TimeoutError like a real socket."""

    def __init__(self, first: bytes) -> None:
        self._first = first
        self.timeouts: list[float | None] = []
        self.sent = b""
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.sent += data

    def shutdown(self, _how):
        pass

    def recv(self, _n):
        if self._first:
            out, self._first = self._first, b""
            return out
        raise TimeoutError("timed out")

    def close(self):
        self.closed = True


def _container(raw):
    api = MagicMock()
    api.exec_create.return_value = {"Id": "exec-long"}
    api.exec_start.return_value = raw
    api.exec_inspect.return_value = {"ExitCode": None, "Running": True}
    container = MagicMock()
    container.id = "container-long"
    container.client.api = api
    return container


@pytest.mark.asyncio
async def test_a_silent_long_script_returns_still_running_with_its_output():
    from orchestrator import sandbox_manager

    raw = _SilentAfterSocket(_frame(1, b"step 1 done\n"))
    exit_code, stdout, stderr, cwd = await sandbox_manager._exec_with_stdin(
        container=_container(raw),
        wrapped="ignored",
        user="agent",
        env={},
        stdin_data="import time; time.sleep(999)",
        sandbox_id="sbx-long",
        effective_cwd="/home/agent",
        timeout=1,
    )

    assert exit_code == sandbox_manager.EXEC_STILL_RUNNING_EXIT_CODE == 124
    assert "step 1 done" in stdout
    assert "still running after 1s" in stderr and "NOT stopped" in stderr
    assert cwd == "/home/agent"
    assert raw.closed is True


@pytest.mark.asyncio
async def test_the_wait_is_the_callers_timeout_not_dockers_sixty_seconds():
    from orchestrator import sandbox_manager

    raw = _SilentAfterSocket(_frame(1, b"x\n"))
    await sandbox_manager._exec_with_stdin(
        container=_container(raw), wrapped="ignored", user="agent", env={},
        stdin_data="", sandbox_id="sbx-long-2", effective_cwd="/home/agent", timeout=300,
    )
    assert raw.timeouts, "the socket's timeout must be set from the caller's timeout"
    assert raw.timeouts[0] > 290  # the requested 300 s, never the inherited 60 s


@pytest.mark.asyncio
async def test_exec_in_sandbox_passes_its_timeout_to_the_stdin_path(monkeypatch):
    from orchestrator import sandbox_manager

    seen = {}

    async def fake(*args, **kwargs):
        seen.update(kwargs)
        return 0, "", "", "/home/agent"

    monkeypatch.setattr(sandbox_manager, "_exec_with_stdin", fake)
    store = MagicMock()

    async def get(_sid):
        return MagicMock(container_id="c", sandbox_id="sbx-t")

    store.get = get
    monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)

    async def knob(_key):
        return 100000

    monkeypatch.setattr(sandbox_manager, "knob_int", knob)
    client = MagicMock()
    client.containers.get.return_value = MagicMock(status="running")
    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: client)

    await sandbox_manager.exec_in_sandbox("sbx-t", "python3 -", timeout=240, stdin="print(1)")
    assert seen.get("timeout") == 240
