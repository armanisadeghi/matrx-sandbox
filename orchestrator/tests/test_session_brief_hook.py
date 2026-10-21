"""The binding-time kick of the in-box repo tidy + session brief (tracker 1734465c)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from orchestrator import session_brief_hook
from orchestrator.knobs import KnobNotRegisteredError
from orchestrator.models import SandboxResponse, SandboxStatus


def _sandbox() -> SandboxResponse:
    return SandboxResponse(
        sandbox_id="sbx-brief", user_id="00000000-0000-0000-0000-000000000001", organization_id="22222222-2222-4222-8222-222222222222", status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc), template="bare",
    )


@pytest.fixture
def knob(monkeypatch):
    k = AsyncMock(return_value=True)
    monkeypatch.setattr(session_brief_hook, "knob_bool", k)
    monkeypatch.setattr(session_brief_hook.activity, "is_migrating", lambda _sid: False)
    return k


@pytest.mark.asyncio
async def test_kick_runs_detached_as_agent_and_preserves_cwd(monkeypatch, knob):
    execute = AsyncMock(return_value=(0, "started\n", "", "/home/agent"))
    monkeypatch.setattr(session_brief_hook.sandbox_manager, "exec_in_sandbox", execute)
    session_brief_hook.sandbox_manager._sandbox_cwd["sbx-brief"] = "/home/agent/repos/aidream"
    result = await session_brief_hook.kick_session_brief(_sandbox())
    assert result["status"] == "started"
    kwargs = execute.await_args.kwargs
    assert kwargs["user"] == "agent"
    assert "--detach" in kwargs["command"] and kwargs["timeout"] <= 30
    assert session_brief_hook.sandbox_manager._sandbox_cwd["sbx-brief"] == "/home/agent/repos/aidream"
    session_brief_hook.sandbox_manager._sandbox_cwd.pop("sbx-brief", None)


@pytest.mark.asyncio
async def test_old_sdk_reports_unsupported(monkeypatch, knob):
    monkeypatch.setattr(session_brief_hook.sandbox_manager, "exec_in_sandbox",
                        AsyncMock(return_value=(0, "unsupported\n", "", "/home/agent")))
    assert (await session_brief_hook.kick_session_brief(_sandbox()))["status"] == "unsupported"


@pytest.mark.asyncio
async def test_knob_off_or_missing_never_execs(monkeypatch, knob):
    execute = AsyncMock()
    monkeypatch.setattr(session_brief_hook.sandbox_manager, "exec_in_sandbox", execute)
    knob.return_value = False
    assert (await session_brief_hook.kick_session_brief(_sandbox()))["status"] == "disabled"
    knob.side_effect = KnobNotRegisteredError("infrastructure.sandbox", session_brief_hook.KNOB)
    assert (await session_brief_hook.kick_session_brief(_sandbox()))["status"] == "unavailable"
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_exec_failure_is_never_fatal(monkeypatch, knob):
    monkeypatch.setattr(session_brief_hook.sandbox_manager, "exec_in_sandbox",
                        AsyncMock(side_effect=RuntimeError("container gone")))
    result = await session_brief_hook.kick_session_brief(_sandbox())
    assert result["status"] == "failed" and "container gone" in result["reason"]
