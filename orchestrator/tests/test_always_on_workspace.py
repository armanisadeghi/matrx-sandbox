"""An enrolled person's workspace box is ALWAYS ON, or something is fixing it.

There is no "switching in and out of the sandbox". The mark
``labels->>'always_on' = 'true'`` on ``public.sandbox_instances`` is the whole
contract between aidream and this orchestrator; these guards are the four ways
that contract silently stops holding:

  * the TTL sweep expires the box out from under the person,
  * the retention sweep soft-deletes it, which makes it UNRESUMABLE — a
    recoverable outage converted into permanent data loss,
  * a box that is down stays down because nothing revives it, or is revived
    every 60 seconds forever because the brake broke,
  * a Docker daemon restart leaves it exited because its restart policy is
    ``no``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from orchestrator.always_on import ALWAYS_ON_SQL_IS_NOT, is_always_on, revive_always_on
from orchestrator.hosted_migration import HostedMigrationJournal
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore

USER = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
ORG = "33333333-3333-4333-8333-333333333333"

ALWAYS_ON = {"always_on": "true"}


def _box(
    sandbox_id: str,
    status: SandboxStatus,
    *,
    labels: dict | None = None,
    user_id: str = USER,
    organization_id: str = ORG,
    age: timedelta = timedelta(hours=1),
    tier: str = "hosted",
    stopped_ago: timedelta | None = None,
) -> SandboxResponse:
    now = datetime.now(timezone.utc)
    return SandboxResponse(
        sandbox_id=sandbox_id,
        user_id=user_id,
        organization_id=organization_id,
        status=status,
        created_at=now - age,
        stopped_at=(now - stopped_ago) if stopped_ago is not None else None,
        expires_at=now - timedelta(minutes=5),
        ttl_seconds=7200,
        tier=tier,
        labels=dict(labels) if labels else None,
    )


# ── The mark itself ──────────────────────────────────────────────────────────

def test_the_python_predicate_matches_the_sql_one_exactly() -> None:
    """``jsonb ->> 'x'`` renders the JSON string "true" and the JSON boolean
    true identically. Anything else is not the mark — in BOTH spellings, or
    the sweeps and the revive disagree about which boxes exist."""
    assert is_always_on({"always_on": "true"}) is True
    assert is_always_on({"always_on": True}) is True
    for not_marked in ({}, None, {"always_on": "false"}, {"always_on": "True"},
                       {"always_on": 1}, {"other": "true"}, "true"):
        assert is_always_on(not_marked) is False, not_marked


# ── 1. The TTL sweep ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_expire_stale_never_expires_an_always_on_box() -> None:
    """Live, this is the person's workspace torn down on a clock that should
    never have applied to it."""
    store = InMemorySandboxStore()
    await store.save(_box("sbx-always", SandboxStatus.RUNNING, labels=ALWAYS_ON))
    await store.save(_box("sbx-ordinary", SandboxStatus.RUNNING))

    expired = await store.expire_stale(tier="hosted")

    assert "sbx-always" not in expired, (
        "the TTL sweep expired an always-on workspace; the person's box is now "
        "down and they never asked for it to stop"
    )
    assert "sbx-ordinary" in expired, "ordinary TTL expiry must still work"


def test_the_postgres_ttl_sweep_carries_the_same_exclusion() -> None:
    """The two store implementations must not disagree about the mark."""
    import inspect

    from orchestrator.store import PostgresSandboxStore

    source = inspect.getsource(PostgresSandboxStore.expire_stale)
    assert ALWAYS_ON_SQL_IS_NOT in source, (
        "PostgresSandboxStore.expire_stale does not exclude always-on rows, so "
        "production behaves differently from every test in this file"
    )


# ── 2. The retention sweep ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retention_never_soft_deletes_an_always_on_workspace() -> None:
    """A soft-deleted row cannot be resumed (``/resume`` 409s on
    ``deleted_at``), so purging an always-on person's parked workspace turns a
    recoverable outage into permanent data loss."""
    store = InMemorySandboxStore()
    await store.save(_box("sbx-always", SandboxStatus.STOPPED, labels=ALWAYS_ON,
                          age=timedelta(days=40), stopped_ago=timedelta(days=30)))
    await store.save(_box("sbx-ordinary", SandboxStatus.STOPPED,
                          age=timedelta(days=40), stopped_ago=timedelta(days=30)))

    purged = await store.purge_terminal_older_than(7)

    assert "sbx-always" not in purged, (
        "retention soft-deleted an always-on workspace; it can never be "
        "resumed again and the person's home is unreachable"
    )
    assert "sbx-ordinary" in purged, "ordinary retention must still work"


def test_the_postgres_retention_sweep_carries_the_same_exclusion() -> None:
    import inspect

    from orchestrator.store import PostgresSandboxStore

    source = inspect.getsource(PostgresSandboxStore.purge_terminal_older_than)
    assert ALWAYS_ON_SQL_IS_NOT in source, (
        "PostgresSandboxStore.purge_terminal_older_than does not exclude "
        "always-on rows"
    )


# ── 3. The revive pass ───────────────────────────────────────────────────────

def _wire_revive(monkeypatch, tmp_path, resumed: list[str], *, tier: str = "hosted"):
    """Keep ``revive_always_on`` real; replace only its external boundaries."""
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr(
        "orchestrator.hosted_operation_lease.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr(
        "orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    monkeypatch.setattr("orchestrator.config.settings.host_tier", tier)
    monkeypatch.setattr("orchestrator.hosted_operation_lease.settings.host_tier", tier)
    monkeypatch.setattr("orchestrator.home_identity.settings.host_tier", tier)

    async def fake_resume(sandbox_id: str):
        resumed.append(sandbox_id)
        return SimpleNamespace(sandbox_id=f"{sandbox_id}-revived")

    monkeypatch.setattr("orchestrator.routes.sandboxes.resume_sandbox", fake_resume)


@pytest.mark.asyncio
async def test_a_down_always_on_box_is_brought_back(monkeypatch, tmp_path) -> None:
    resumed: list[str] = []
    _wire_revive(monkeypatch, tmp_path, resumed)
    store = InMemorySandboxStore()
    await store.save(_box("sbx-down", SandboxStatus.STOPPED, labels=ALWAYS_ON,
                          age=timedelta(hours=2)))

    summary = await revive_always_on(store)

    assert resumed == ["sbx-down"], (
        "a marked workspace was down and the reaper tick left it down — the "
        "person has no box and nothing is repairing it"
    )
    assert summary["revived"] == ["sbx-down-revived"]


@pytest.mark.asyncio
async def test_an_unmarked_down_box_is_left_alone(monkeypatch, tmp_path) -> None:
    """Reviving an ordinary stopped box would resurrect every workspace anyone
    ever deliberately stopped."""
    resumed: list[str] = []
    _wire_revive(monkeypatch, tmp_path, resumed)
    store = InMemorySandboxStore()
    await store.save(_box("sbx-plain", SandboxStatus.STOPPED, age=timedelta(hours=2)))

    await revive_always_on(store)

    assert resumed == []


@pytest.mark.asyncio
async def test_another_tiers_rows_are_never_touched(monkeypatch, tmp_path) -> None:
    """``sandbox_instances`` is shared. A hosted orchestrator spawning an ec2
    row's container would attach a home that lives on another host."""
    resumed: list[str] = []
    _wire_revive(monkeypatch, tmp_path, resumed, tier="hosted")
    store = InMemorySandboxStore()
    await store.save(_box("sbx-ec2", SandboxStatus.STOPPED, labels=ALWAYS_ON,
                          tier="ec2", age=timedelta(hours=2)))

    await revive_always_on(store)

    assert resumed == []


@pytest.mark.asyncio
async def test_a_pair_that_already_has_a_live_box_is_skipped(monkeypatch, tmp_path) -> None:
    """Somebody already brought it back. A second box burns the person's
    admission slot and splits their work across two machines."""
    resumed: list[str] = []
    _wire_revive(monkeypatch, tmp_path, resumed)
    store = InMemorySandboxStore()
    # The person's box has been up for five hours; a create attempt two hours
    # ago failed and left the NEWEST row in `failed`. Reviving on that row
    # would hand them a second machine while the first is still running.
    await store.save(_box("sbx-running", SandboxStatus.READY, labels=ALWAYS_ON,
                          age=timedelta(hours=5)))
    await store.save(_box("sbx-failed-attempt", SandboxStatus.FAILED,
                          labels=ALWAYS_ON, age=timedelta(hours=2)))

    summary = await revive_always_on(store)

    assert resumed == [], "a second workspace was spawned for a person who has one"
    assert summary["skipped_live"] == 1


@pytest.mark.asyncio
async def test_only_the_newest_row_per_person_and_org_is_considered(
        monkeypatch, tmp_path) -> None:
    """A person's history is a chain of dead rows; reviving all of them would
    spawn one box per row."""
    resumed: list[str] = []
    _wire_revive(monkeypatch, tmp_path, resumed)
    store = InMemorySandboxStore()
    for n, age in enumerate([timedelta(days=3), timedelta(days=2), timedelta(hours=4)]):
        await store.save(_box(f"sbx-{n}", SandboxStatus.EXPIRED, labels=ALWAYS_ON, age=age))

    await revive_always_on(store)

    assert resumed == ["sbx-2"], "only the newest workspace row names the current home"


@pytest.mark.asyncio
async def test_the_minimum_interval_is_the_only_loop_brake_and_is_unconditional(
        monkeypatch, tmp_path) -> None:
    """THE RUNAWAY GUARD. A box that fails to boot writes a fresh row each
    attempt; without this brake the reaper resurrects it every 60 seconds
    forever. The knob is 180s, so a row 30 seconds old is not touched."""
    resumed: list[str] = []
    _wire_revive(monkeypatch, tmp_path, resumed)
    store = InMemorySandboxStore()
    await store.save(_box("sbx-justfailed", SandboxStatus.FAILED, labels=ALWAYS_ON,
                          age=timedelta(seconds=30)))

    summary = await revive_always_on(store)

    assert resumed == [], (
        "a workspace that died 30s ago was revived immediately — this is the "
        "every-60-seconds resurrection loop"
    )
    assert summary["skipped_recent"] == 1


@pytest.mark.asyncio
async def test_a_pass_is_capped_and_says_what_it_left_behind(monkeypatch, tmp_path) -> None:
    """Never a silent truncation: the log is the difference between "working
    through a backlog" and "we forgot about you"."""
    resumed: list[str] = []
    _wire_revive(monkeypatch, tmp_path, resumed)
    from tests.conftest import seed_sandbox_knobs
    seed_sandbox_knobs({"always_on_revive_max_per_pass": 2})
    store = InMemorySandboxStore()
    for n in range(5):
        await store.save(_box(
            f"sbx-{n}", SandboxStatus.STOPPED, labels=ALWAYS_ON,
            user_id=f"4444444{n}-4444-4444-8444-444444444444",
            age=timedelta(hours=2 + n),
        ))

    summary = await revive_always_on(store)

    assert len(resumed) == 2, f"cap not honoured: {resumed}"
    assert summary["left_for_next_tick"] == 3
    # Longest-down first, so nobody starves behind a newer outage.
    assert resumed == ["sbx-4", "sbx-3"]


@pytest.mark.asyncio
async def test_one_failing_revive_never_ends_the_pass(monkeypatch, tmp_path) -> None:
    resumed: list[str] = []
    _wire_revive(monkeypatch, tmp_path, resumed)

    async def exploding_resume(sandbox_id: str):
        if sandbox_id == "sbx-bad":
            raise RuntimeError("no image")
        resumed.append(sandbox_id)
        return SimpleNamespace(sandbox_id=f"{sandbox_id}-revived")

    monkeypatch.setattr("orchestrator.routes.sandboxes.resume_sandbox", exploding_resume)
    store = InMemorySandboxStore()
    await store.save(_box("sbx-bad", SandboxStatus.FAILED, labels=ALWAYS_ON,
                          age=timedelta(hours=9)))
    await store.save(_box("sbx-good", SandboxStatus.STOPPED, labels=ALWAYS_ON,
                          user_id=USER_B, age=timedelta(hours=2)))

    summary = await revive_always_on(store)

    assert resumed == ["sbx-good"]
    assert summary["failed"] == 1


@pytest.mark.asyncio
async def test_the_reaper_tick_runs_the_revive_pass(monkeypatch) -> None:
    """No new scheduler and no new loop: the existing 60s tick does it, or the
    whole feature is a function nothing calls."""
    from contextlib import ExitStack

    from orchestrator import reaper

    class _Store:
        async def list(self): return []
        async def expire_stale(self, *, tier, include_sandbox_ids): return []
        async def purge_terminal_older_than(self, _days): return []

    called: list[str] = []

    async def fake_revive(store):
        called.append("revive")
        return {"revived": ["sbx-x-revived"], "failed": 0, "left_for_next_tick": 0,
                "candidates": 1, "skipped_live": 0, "skipped_recent": 0}

    async def _empty(_store): return []
    async def _healthy(_store): return {"stopped": [], "refreshed": 0}

    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: _Store())
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "hosted")
    monkeypatch.setattr(reaper, "_lease_reaper_fleet", lambda _t: (ExitStack(), set()))
    monkeypatch.setattr("orchestrator.reconcile.reap_zombie_containers", _empty)
    monkeypatch.setattr("orchestrator.reconcile.reconcile_liveness", _healthy)
    monkeypatch.setattr("orchestrator.always_on.revive_always_on", fake_revive)

    summary = await reaper._reap_once()

    assert called == ["revive"], (
        "the reaper tick does not run the always-on revive pass; a down "
        "workspace stays down forever"
    )
    assert summary["always_on_revived"] == 1


# ── 4. Surviving a Docker daemon restart ─────────────────────────────────────

def test_an_always_on_box_restarts_with_the_daemon() -> None:
    """Every non-``development`` box (including ``slim``, the Personal Staff
    default) is created with ``restart_policy=no``, so a daemon restart or a
    host reboot leaves it exited and the next liveness pass writes it
    ``stopped``. The mark has to change that."""
    from orchestrator.sandbox_manager import container_restart_policy

    assert container_restart_policy("slim", ALWAYS_ON)["Name"] == "unless-stopped", (
        "an always-on box would stay exited after a Docker daemon restart"
    )
    assert container_restart_policy("development", None)["Name"] == "unless-stopped"
    assert container_restart_policy("slim", None)["Name"] == "no"
    assert container_restart_policy("slim", {"always_on": "false"})["Name"] == "no"
