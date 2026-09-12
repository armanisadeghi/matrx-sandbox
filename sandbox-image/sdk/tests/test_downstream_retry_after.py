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
    assert waits == [27.0], "a shed poll waits what the server asked, not the local backoff"
