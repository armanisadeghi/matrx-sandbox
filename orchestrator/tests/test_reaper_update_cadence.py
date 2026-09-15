"""The version-drift check is deliberately slower than lifecycle reaping."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock

import pytest

from orchestrator import reaper


class _ReaperStore:
    """External persistence boundary for a reaper pass with no expired rows."""

    def __init__(self) -> None:
        self.expiry_ticks = 0

    async def list(self):
        return []

    async def expire_stale(self, *, tier, include_sandbox_ids):
        self.expiry_ticks += 1
        return []

    async def purge_terminal_older_than(self, _days):
        return []


def _wire_reaper(monkeypatch, store: _ReaperStore, drift_scans: list[float], clock: list[float]):
    """Keep `_reap_once` real while replacing its external boundaries."""
    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: store)
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "hosted")
    monkeypatch.setattr(reaper, "_lease_reaper_fleet", lambda _targets: (ExitStack(), set()))
    monkeypatch.setattr(
        "orchestrator.reconcile.reap_zombie_containers",
        lambda _store: _empty_zombies(),
    )
    monkeypatch.setattr(
        "orchestrator.reconcile.reconcile_liveness",
        lambda _store: _healthy_liveness(),
    )
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: object())

    def drift_summary(_docker):
        drift_scans.append(clock[0])
        return {"drifted": []}

    monkeypatch.setattr("orchestrator.versioning.drift_summary", drift_summary)
    monkeypatch.setattr(reaper, "_auto_migrate_enabled", _auto_migrate_off)
    monkeypatch.setattr(reaper, "_last_update_check_at", None, raising=False)
    monkeypatch.setattr(reaper.time, "monotonic", lambda: clock[0])


async def _empty_zombies():
    return []


async def _healthy_liveness():
    return {"stopped": [], "refreshed": 0}


async def _auto_migrate_off():
    return False


@pytest.mark.asyncio
async def test_update_checks_coalesce_while_expiry_still_runs_each_reaper_tick(monkeypatch):
    """Break caught: drift scans ran at the 60-second expiry cadence."""
    store = _ReaperStore()
    drift_scans: list[float] = []
    clock = [1_000.0]
    _wire_reaper(monkeypatch, store, drift_scans, clock)

    async def knob_int(key: str) -> int:
        return {
            "terminal_retention_days": 7,
            "auto_update_check_interval_seconds": 600,
        }[key]

    monkeypatch.setattr(reaper, "knob_int", knob_int)

    for tick in (1_000.0, 1_060.0, 1_599.0, 1_600.0):
        clock[0] = tick
        await reaper._reap_once()

    assert drift_scans == [1_000.0, 1_600.0]
    assert store.expiry_ticks == 4


@pytest.mark.asyncio
async def test_unreadable_update_interval_skips_drift_but_not_expiry(monkeypatch, caplog):
    """Break caught: a missing cadence row silently fell back to a 60-second scan."""
    store = _ReaperStore()
    drift_scans: list[float] = []
    clock = [1_000.0]
    _wire_reaper(monkeypatch, store, drift_scans, clock)

    async def knob_int(key: str) -> int:
        if key == "auto_update_check_interval_seconds":
            raise RuntimeError("feature knob row unavailable")
        assert key == "terminal_retention_days"
        return 7

    monkeypatch.setattr(reaper, "knob_int", knob_int)

    await reaper._reap_once()

    assert drift_scans == []
    assert store.expiry_ticks == 1
    assert "auto-update check interval unreadable" in caplog.text


@pytest.mark.asyncio
async def test_automatic_reaper_uses_the_batch_that_owns_quiet_policy(monkeypatch):
    """The automatic caller reaches migrate_all, not a shortcut around its fences."""
    store = _ReaperStore()
    clock = [1_000.0]
    _wire_reaper(monkeypatch, store, [], clock)

    async def knob_int(key: str) -> int:
        return {
            "terminal_retention_days": 7,
            "auto_update_check_interval_seconds": 600,
            "migrate_max_per_pass": 2,
        }[key]

    async def auto_migrate_on() -> bool:
        return True

    migrate_all = AsyncMock(return_value={
        "migrated": [], "deferred": ["quiet"], "failed": [],
        "skipped": [], "unsupported": [],
    })
    monkeypatch.setattr(reaper, "knob_int", knob_int)
    monkeypatch.setattr(reaper, "_auto_migrate_enabled", auto_migrate_on)
    monkeypatch.setattr(
        "orchestrator.versioning.drift_summary", lambda _docker: {"drifted": 1},
    )
    monkeypatch.setattr("orchestrator.migrate.migrate_all_drifted", migrate_all)

    summary = await reaper._reap_once()

    migrate_all.assert_awaited_once_with(store=store, max_per_pass=2)
    assert summary["auto_migrated"] == 0
