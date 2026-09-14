"""The polling fallback honours the bridge's Retry-After and never polls in lockstep.

SUT: ``matrx_agent.cloud_sync.downstream`` — ``_retry_after_seconds``,
``_jittered`` and ``PollingSubscriber._loop``.

Live evidence (2026-09-12): 43 sandboxes polled ``/api/cloud-files/changes``
in the same second, three times in one evening, and consumed AI Dream's
connection pool. The bridge now sheds such herds with ``503`` + ``Retry-After``.

Breaks these tests name:
* a shed poll (503 + Retry-After) is retried on the local backoff instead of
  the wait the server asked for;
* the header is trusted blindly (a bogus or huge value stalls the poller);
* the steady-state wait is a constant, so a fleet started together stays in
  lockstep forever.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from matrx_agent.cloud_sync import downstream


def _status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://server.example/api/cloud-files/changes")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError("shed", request=request, response=response)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status_error(503, {"Retry-After": "27"}), 27.0),
        (_status_error(429, {"Retry-After": "3"}), 3.0),
        (_status_error(503), None),
        (_status_error(503, {"Retry-After": "soon"}), None),
        (_status_error(503, {"Retry-After": "0"}), None),
        (_status_error(503, {"Retry-After": "99999"}), downstream.POLL_BACKOFF_MAX),
        (_status_error(500, {"Retry-After": "27"}), None),
        (RuntimeError("not http"), None),
    ],
)
def test_retry_after_is_read_only_from_shed_responses_and_clamped(error, expected) -> None:
    assert downstream._retry_after_seconds(error) == expected


def test_jitter_stays_inside_the_declared_band_and_varies() -> None:
    base = downstream.POLL_INTERVAL_SECONDS
    spread = base * downstream.POLL_JITTER_FRACTION
    samples = {downstream._jittered(base) for _ in range(200)}
    assert all(base - spread <= s <= base + spread for s in samples)
    assert len(samples) > 1, "a constant wait keeps a fleet in lockstep"
    assert downstream._jittered(0.0) == 1.0


class _ShedThenStopClient:
    def __init__(self) -> None:
        self.calls = 0

    async def list_changes(self, since_iso: str) -> dict:
        self.calls += 1
        raise _status_error(503, {"Retry-After": "27"})


@pytest.mark.asyncio
async def test_loop_waits_exactly_the_retry_after_the_bridge_asked_for(monkeypatch) -> None:
    client = _ShedThenStopClient()
    subscriber = downstream.PollingSubscriber(client)
    waits: list[float] = []

    async def fake_wait_for(awaitable, timeout):
        waits.append(timeout)
        awaitable.close()
        subscriber._stop.set()
        raise asyncio.TimeoutError

    monkeypatch.setattr(downstream.asyncio, "wait_for", fake_wait_for)

    async def on_change(change):  # pragma: no cover - never reached
        raise AssertionError("no change expected")

    await subscriber._loop(on_change)

    assert client.calls == 1
    assert len(waits) == 1
    spread = 27.0 * downstream.POLL_JITTER_FRACTION
    assert 27.0 - spread <= waits[0] <= 27.0 + spread, (
        "a shed poll waits what the server asked (jittered), not the local backoff"
    )


# ── The server's cadence instruction on a SUCCESSFUL answer ────────────────
#
# Breaks these tests name: the box keeps its own 30 s timer while the bridge is
# asking the whole fleet to slow to 45 s (2026-09-14: 226 boxes arriving at
# 7.5/s into a feed that shed 61 polls across 49 users in one minute), or it
# follows an absurd instruction off a cliff (a 0 s cadence is a hammer, an
# hour-long one is a box that has silently stopped syncing).


class _AnnotatedClient:
    """A bridge that answers normally and says when to come back."""

    def __init__(self, poll_after) -> None:
        self.poll_after = poll_after
        self.calls = 0

    async def list_changes(self, since_iso: str) -> dict:
        self.calls += 1
        envelope = {"files": [], "next_cursor": since_iso}
        if self.poll_after is not None:
            envelope["poll_after_seconds"] = self.poll_after
        return envelope


async def _one_cycle(client) -> float:
    """Run exactly one poll and return the wait it chose afterwards."""
    subscriber = downstream.PollingSubscriber(client)
    waits: list[float] = []
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable, timeout):
        waits.append(timeout)
        awaitable.close()
        subscriber._stop.set()
        raise asyncio.TimeoutError

    downstream.asyncio.wait_for = fake_wait_for
    try:
        await subscriber._loop(lambda change: asyncio.sleep(0))
    finally:
        downstream.asyncio.wait_for = real_wait_for
    assert len(waits) == 1
    return waits[0]


@pytest.mark.asyncio
async def test_the_loop_follows_the_interval_the_bridge_asked_for() -> None:
    wait = await _one_cycle(_AnnotatedClient(45))
    spread = 45.0 * downstream.POLL_JITTER_FRACTION
    assert 45.0 - spread <= wait <= 45.0 + spread, (
        "a box that ignores the server's cadence is a box that keeps arriving "
        "into a feed already shedding"
    )


@pytest.mark.asyncio
async def test_an_answer_without_an_instruction_keeps_the_built_in_cadence() -> None:
    """An older bridge must not change this loop's behaviour."""
    wait = await _one_cycle(_AnnotatedClient(None))
    spread = downstream.POLL_INTERVAL_SECONDS * downstream.POLL_JITTER_FRACTION
    assert (
        downstream.POLL_INTERVAL_SECONDS - spread
        <= wait
        <= downstream.POLL_INTERVAL_SECONDS + spread
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("absurd", [0, 0.5, 3, 4000, "soon", None])
async def test_an_instruction_outside_the_accepted_band_is_refused_not_clamped(absurd) -> None:
    wait = await _one_cycle(_AnnotatedClient(absurd))
    spread = downstream.POLL_INTERVAL_SECONDS * downstream.POLL_JITTER_FRACTION
    assert (
        downstream.POLL_INTERVAL_SECONDS - spread
        <= wait
        <= downstream.POLL_INTERVAL_SECONDS + spread
    ), "an instruction this image cannot accept leaves the cadence alone"


@pytest.mark.asyncio
async def test_the_instruction_survives_the_next_cycle() -> None:
    client = _AnnotatedClient(45)
    subscriber = downstream.PollingSubscriber(client)
    subscriber._adopt_server_interval(45)
    assert subscriber._interval_seconds == 45.0
    # A later answer that carries no instruction does not reset the cadence.
    subscriber._adopt_server_interval(None)
    assert subscriber._interval_seconds == 45.0
    subscriber._adopt_server_interval(30)
    assert subscriber._interval_seconds == 30.0
