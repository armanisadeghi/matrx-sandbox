"""The TTL is an idle ceiling, and the code finally says what the docs said.

THE DEFECT THIS CLOSES. ``models.py`` ("heartbeats roll expires_at forward on
every ping; the TTL is the idle ceiling, not a hard wall-clock limit") and
``reaper.py`` both documented a heartbeat-refreshed ceiling. The store stamped
``last_heartbeat_at`` and nothing else. So a person working in a box for three
hours on a two-hour TTL had it torn down mid-sentence, exactly as though they
had walked away — and every document said that could not happen. Fixed by
making the CODE true: for a personal box, activity is the reason to keep it.

The guards below are the two ways that fix can silently rot: the extension
stops happening, or it starts happening to rows it must never touch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore

USER = "00000000-0000-0000-0000-0000000000bb"
ORG = "00000000-0000-0000-0000-0000000000cc"


def _box(status: SandboxStatus, expires_in: timedelta | None) -> SandboxResponse:
    now = datetime.now(timezone.utc)
    return SandboxResponse(
        sandbox_id="sbx-ttl",
        user_id=USER,
        organization_id=ORG,
        status=status,
        created_at=now,
        ttl_seconds=7200,
        expires_at=(now + expires_in) if expires_in is not None else None,
    )


async def _store_with(box: SandboxResponse) -> InMemorySandboxStore:
    store = InMemorySandboxStore()
    await store.save(box)
    return store


@pytest.mark.asyncio
async def test_a_heartbeat_pushes_expiry_a_full_ttl_into_the_future() -> None:
    """The box is 10 minutes from death; one ping buys it its whole TTL again."""
    box = _box(SandboxStatus.RUNNING, timedelta(minutes=10))
    store = await _store_with(box)

    assert await store.update_heartbeat("sbx-ttl", extend_ttl=True) is True

    after = await store.get("sbx-ttl")
    remaining = (after.expires_at - datetime.now(timezone.utc)).total_seconds()
    assert remaining > 7000, (
        f"expiry is still {remaining:.0f}s away. Live, this is the box torn "
        "down under a person who was using it."
    )


@pytest.mark.asyncio
async def test_the_knob_off_keeps_the_hard_wall_clock() -> None:
    """The other setting is a real setting, not decoration."""
    box = _box(SandboxStatus.RUNNING, timedelta(minutes=10))
    store = await _store_with(box)
    before = (await store.get("sbx-ttl")).expires_at

    await store.update_heartbeat("sbx-ttl", extend_ttl=False)

    assert (await store.get("sbx-ttl")).expires_at == before
    assert (await store.get("sbx-ttl")).last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_a_terminal_box_is_never_resurrected_by_a_late_ping() -> None:
    """A container on its way out still pings. Extending a stopped row would
    hand it an expiry in the future and make a dead box look schedulable."""
    for status in (SandboxStatus.STOPPED, SandboxStatus.EXPIRED, SandboxStatus.FAILED):
        box = _box(status, timedelta(minutes=-5))
        box.sandbox_id = f"sbx-{status.value}"
        store = await _store_with(box)
        before = (await store.get(box.sandbox_id)).expires_at

        await store.update_heartbeat(box.sandbox_id, extend_ttl=True)

        assert (await store.get(box.sandbox_id)).expires_at == before, status


@pytest.mark.asyncio
async def test_a_box_whose_ttl_clock_never_started_is_left_alone() -> None:
    """``expires_at`` is set by the DB trigger on the first live transition.
    Inventing one here would start a clock the platform never started."""
    box = _box(SandboxStatus.CREATING, None)
    store = await _store_with(box)

    await store.update_heartbeat("sbx-ttl", extend_ttl=True)

    assert (await store.get("sbx-ttl")).expires_at is None
