"""The one downstream transport, and deletions on it.

What was wrong at HEAD: ``make_subscriber`` chose between a direct
Supabase-Realtime subscriber (a WebSocket to the PLATFORM DATABASE, scoped by a
client-side owner filter, behind five Supabase key names the orchestrator never
injects) and a poller — and when Realtime was unavailable, which was always, it
fell back to polling WITHOUT SAYING SO. Meanwhile the poller could not carry
deletions at all, so a file deleted in AI Dream stayed on disk here and the
shutdown up-sync resurrected it in the cloud.

These tests hold the fix: one transport, announced; deletion markers honoured
the moment the bridge sends them; and a bridge that cannot send them makes the
sandbox state the consequence out loud.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from matrx_agent.cloud_sync import downstream
from matrx_agent.cloud_sync.downstream import PollingSubscriber, RemoteChange, make_subscriber

DOWNSTREAM_SOURCE = Path(downstream.__file__)


class _FakeClient:
    """A bridge client that answers one prepared change-feed envelope."""

    def __init__(self, envelope: dict) -> None:
        self._envelope = envelope
        self.calls = 0

    async def list_changes(self, since_iso: str, limit: int = 1000) -> dict:
        self.calls += 1
        return dict(self._envelope)


def _drain(subscriber: PollingSubscriber, client: _FakeClient) -> list[RemoteChange]:
    """Run exactly one polling cycle and collect what it dispatched."""
    seen: list[RemoteChange] = []

    async def on_change(change: RemoteChange) -> None:
        seen.append(change)
        if client.calls >= 1:
            subscriber._stop.set()

    async def run() -> None:
        await asyncio.wait_for(subscriber._loop(on_change), timeout=5)

    asyncio.run(run())
    return seen


def test_the_sandbox_never_subscribes_to_the_platform_database() -> None:
    """The direct-database path is DELETED, not disabled — including the env
    ladder that would let somebody switch it back on with a leaked key."""
    source = DOWNSTREAM_SOURCE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("#", "*"))
    )
    # The module docstring explains the deletion, so look at the code below it.
    code = code.split('"""', 2)[-1]

    for forbidden in (
        "AsyncRealtimeClient",
        "on_postgres_changes",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_MATRIX_KEY",
        "_supabase_creds",
        "class RealtimeSubscriber",
    ):
        assert forbidden not in code, f"{forbidden} is back in downstream.py"


def test_make_subscriber_returns_the_one_transport_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="matrx_agent.cloud_sync.downstream"):
        sub = make_subscriber(_FakeClient({"files": []}), cfg=None)

    assert isinstance(sub, PollingSubscriber)
    assert "downstream transport" in caplog.text
    assert "never subscribes to the platform database" in caplog.text


def test_a_deleted_row_becomes_a_delete_not_a_download() -> None:
    """The contract: ``deleted: true`` on a change-feed row."""
    client = _FakeClient({
        "files": [
            {"file_path": "notes.md", "updated_at": "2026-09-17T00:00:01Z"},
            {"file_path": "gone.md", "deleted": True, "updated_at": "2026-09-17T00:00:02Z"},
        ],
        "next_cursor": "2026-09-17T00:00:02Z",
        "deletions_supported": True,
    })

    changes = _drain(PollingSubscriber(client), client)

    assert [(c.kind, c.rel_path) for c in changes] == [
        ("modified", "notes.md"),
        ("deleted", "gone.md"),
    ]


def test_a_soft_deleted_row_is_a_delete_too() -> None:
    """``deleted_at`` is the column the soft-delete writes; a row carrying it
    can never be a modification, so it is honoured as the same statement."""
    client = _FakeClient({
        "files": [{"file_path": "gone.md", "deleted_at": "2026-09-17T00:00:02Z"}],
        "next_cursor": "2026-09-17T00:00:02Z",
        "deletions_supported": True,
    })

    changes = _drain(PollingSubscriber(client), client)

    assert [(c.kind, c.rel_path) for c in changes] == [("deleted", "gone.md")]


def test_a_bridge_without_deletions_states_the_consequence_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing fails silently: the box says a deleted file will be resurrected
    by the shutdown up-sync, and names the remedy."""
    client = _FakeClient({
        "files": [{"file_path": "notes.md", "updated_at": "2026-09-17T00:00:01Z"}],
        "next_cursor": "2026-09-17T00:00:01Z",
        "deletions_supported": False,
    })
    sub = PollingSubscriber(client)

    with caplog.at_level(logging.WARNING, logger="matrx_agent.cloud_sync.downstream"):
        _drain(sub, client)

    text = caplog.text
    assert "deletions_supported=false" in text
    assert "shutdown up-sync" in text
    assert "deleted=true" in text  # the remedy names the contract
    assert sub._deletions_supported is False

    # Said once per session, not once per poll.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="matrx_agent.cloud_sync.downstream"):
        sub._note_deletion_support(False)
    assert caplog.text == ""


def test_an_older_bridge_that_omits_the_key_is_treated_as_no_deletions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing key is not permission to assume the good case."""
    client = _FakeClient({
        "files": [],
        "next_cursor": "2026-09-17T00:00:01Z",
    })
    sub = PollingSubscriber(client)

    async def run() -> None:
        async def on_change(_change: RemoteChange) -> None:  # pragma: no cover
            raise AssertionError("no rows to dispatch")

        task = asyncio.create_task(sub._loop(on_change))
        await asyncio.sleep(0.05)
        sub._stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    with caplog.at_level(logging.WARNING, logger="matrx_agent.cloud_sync.downstream"):
        asyncio.run(run())

    assert sub._deletions_supported is False
    assert "deletions_supported=false" in caplog.text
