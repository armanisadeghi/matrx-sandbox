"""Unit tests for the migration idle gate (protect recently-active sessions)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.config import settings
from orchestrator.migrate import _has_recent_heartbeat


class _Row:
    def __init__(self, hb):
        self.last_heartbeat_at = hb


def test_no_row_is_not_recent():
    assert _has_recent_heartbeat(None) is False


def test_missing_heartbeat_is_not_recent():
    assert _has_recent_heartbeat(_Row(None)) is False


def test_fresh_heartbeat_is_recent():
    hb = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert _has_recent_heartbeat(_Row(hb)) is True


def test_old_heartbeat_is_not_recent():
    hb = datetime.now(timezone.utc) - timedelta(seconds=settings.migrate_recent_heartbeat_seconds + 60)
    assert _has_recent_heartbeat(_Row(hb)) is False


def test_naive_heartbeat_treated_as_utc():
    # A naive timestamp must not raise; treat it as UTC.
    hb = datetime.utcnow() - timedelta(seconds=5)
    assert _has_recent_heartbeat(_Row(hb)) is True


# ── Widened idle gate (2026-07-09): open sessions + recent tool activity ──────
# An agent between commands and a human with an open terminal both show ZERO
# in-flight calls — the gate must still treat them as busy. The gate now runs
# BEFORE any docker lookups, so these tests need no docker.

import pytest

from orchestrator import activity


@pytest.mark.asyncio
async def test_open_session_defers_migration():
    from orchestrator.migrate import migrate_sandbox
    sid = "sbx-gate-pty00001"
    activity.session_opened(sid)
    try:
        result = await migrate_sandbox(sid, store=None, require_idle=True)
        assert result["status"] == "busy_deferred"
        assert "session" in result["reason"]
    finally:
        activity.session_closed(sid)


@pytest.mark.asyncio
async def test_recent_tool_activity_defers_migration():
    from orchestrator.migrate import migrate_sandbox
    sid = "sbx-gate-recent01"
    activity.note_activity(sid)  # a tool call just finished
    result = await migrate_sandbox(sid, store=None, require_idle=True)
    assert result["status"] == "busy_deferred"
    assert "activity" in result["reason"]


def test_session_refcount_balances():
    sid = "sbx-gate-refcnt01"
    assert activity.open_session_count(sid) == 0
    activity.session_opened(sid)
    activity.session_opened(sid)
    assert activity.open_session_count(sid) == 2
    activity.session_closed(sid)
    assert activity.open_session_count(sid) == 1
    activity.session_closed(sid)
    assert activity.open_session_count(sid) == 0
    activity.session_closed(sid)  # over-close must not go negative
    assert activity.open_session_count(sid) == 0
