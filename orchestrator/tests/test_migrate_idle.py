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
