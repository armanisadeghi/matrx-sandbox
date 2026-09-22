"""``last_heartbeat_at`` means "the last time the PLATFORM OBSERVED this box alive".

🚨 THE DEFECT THIS CLOSES. The in-container heartbeat DOES NOT EXIST.
``sandbox-image/sdk/matrx_agent/client.py::heartbeat()`` is written and nothing
calls it — the "matrx_agent daemon pings every ~60s" that aidream's
``ensure_default_sandbox`` documents as THE liveness signal has never run.
Measured on production 2026-09-22: of 273 ``sandbox_instances`` rows only 9 had
EVER carried a ``last_heartbeat_at``, the newest was two days old, and 223 of
the 226 live rows had none at all. The only sender in the whole platform is a
browser hook that ticks while somebody has the Code workspace open.

The consequence is not cosmetic. aidream treats a box with no heartbeat in ten
minutes as a corpse, so it never reuses one: every contact created a NEW box
(a 364-second cold create instead of a ~4-second reuse), and
``heartbeat_extends_ttl`` — a shipped, operator-visible knob — governed
nothing.

The fix puts the signal where the truth already is. The 60-second liveness
reconcile ALREADY asks Docker which containers are alive and stamps
``updated_at`` on exactly those rows. An observation by the orchestrator is a
STRONGER signal than a container asserting its own health, so that same
statement stamps the heartbeat.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore, PostgresSandboxStore

USER = "55555555-5555-4555-8555-555555555555"
ORG = "66666666-6666-4666-8666-666666666666"


def _box(sandbox_id: str, status: SandboxStatus, container_id: str | None,
         *, expires_in: timedelta | None = timedelta(minutes=10)) -> SandboxResponse:
    now = datetime.now(timezone.utc)
    return SandboxResponse(
        sandbox_id=sandbox_id, user_id=USER, organization_id=ORG, status=status,
        container_id=container_id, created_at=now - timedelta(hours=1),
        ttl_seconds=7200, tier="hosted",
        expires_at=(now + expires_in) if expires_in is not None else None,
    )


@pytest.mark.asyncio
async def test_observing_a_live_container_stamps_the_heartbeat() -> None:
    """Without this the column is dead: 223 of 226 live production rows had
    never carried a value, so every consumer judging liveness by it concluded
    "corpse" and built a second box."""
    store = InMemorySandboxStore()
    await store.save(_box("sbx-alive", SandboxStatus.RUNNING, "c-alive"))

    await store.reconcile({"c-alive"}, tier="hosted")

    after = await store.get("sbx-alive")
    assert after.last_heartbeat_at is not None, (
        "the orchestrator watched this container answer and recorded nothing; "
        "aidream will call the box dead and cold-create a replacement"
    )


@pytest.mark.asyncio
async def test_the_observation_extends_the_ttl_under_the_shipped_knob() -> None:
    """``heartbeat_extends_ttl`` is a shipped knob that governed nothing,
    because the only thing that honoured it never ran."""
    store = InMemorySandboxStore()
    await store.save(_box("sbx-alive", SandboxStatus.RUNNING, "c-alive"))
    from tests.conftest import seed_sandbox_knobs
    seed_sandbox_knobs({"heartbeat_extends_ttl": True})

    await store.reconcile({"c-alive"}, tier="hosted")

    remaining = ((await store.get("sbx-alive")).expires_at
                 - datetime.now(timezone.utc)).total_seconds()
    assert remaining > 7000, (
        f"expiry is still only {remaining:.0f}s away — a box the platform can "
        "see running is being timed out as though it were abandoned"
    )


@pytest.mark.asyncio
async def test_the_knob_off_leaves_the_wall_clock_alone() -> None:
    store = InMemorySandboxStore()
    await store.save(_box("sbx-alive", SandboxStatus.RUNNING, "c-alive"))
    from tests.conftest import seed_sandbox_knobs
    seed_sandbox_knobs({"heartbeat_extends_ttl": False})
    before = (await store.get("sbx-alive")).expires_at

    await store.reconcile({"c-alive"}, tier="hosted")

    after = await store.get("sbx-alive")
    assert after.expires_at == before
    assert after.last_heartbeat_at is not None, "the stamp is not knob-gated"


@pytest.mark.asyncio
async def test_a_vanished_container_is_stopped_and_never_stamped() -> None:
    """An observation is only a heartbeat when the box was actually observed."""
    store = InMemorySandboxStore()
    await store.save(_box("sbx-gone", SandboxStatus.RUNNING, "c-gone"))

    result = await store.reconcile({"c-other"}, tier="hosted")

    after = await store.get("sbx-gone")
    assert "sbx-gone" in result["stopped"]
    assert after.last_heartbeat_at is None
    assert after.stop_reason == "graceful_shutdown"


@pytest.mark.asyncio
async def test_a_sibling_tiers_rows_are_never_reconciled() -> None:
    """The table is shared between the EC2 and hosted orchestrators."""
    store = InMemorySandboxStore()
    ec2 = _box("sbx-ec2", SandboxStatus.RUNNING, "c-elsewhere")
    ec2.tier = "ec2"
    await store.save(ec2)

    result = await store.reconcile({"c-nothing"}, tier="hosted")

    assert result["stopped"] == []
    assert (await store.get("sbx-ec2")).status == SandboxStatus.RUNNING


def test_the_postgres_liveness_update_stamps_the_heartbeat() -> None:
    """The two implementations must not disagree about what a live row gets."""
    source = inspect.getsource(PostgresSandboxStore.reconcile)
    alive_update = source.split("alive_sandbox_ids:", 1)[-1]
    assert "last_heartbeat_at = NOW()" in alive_update, (
        "the Postgres liveness reconcile still refreshes only updated_at, so "
        "production leaves last_heartbeat_at null forever"
    )
    assert "ttl_seconds" in alive_update, (
        "the Postgres liveness reconcile does not apply heartbeat_extends_ttl"
    )


def test_the_meaning_is_written_down_where_the_next_reader_looks() -> None:
    """Four aidream modules judge liveness by this column. The docs said a
    container pings; nothing does. The words have to change with the code."""
    from orchestrator.models import SandboxResponse as Model

    described = Model.model_fields["last_heartbeat_at"].description or ""
    assert "observed" in described.lower(), (
        "models.py still describes last_heartbeat_at as a ping from the "
        "in-container agent, which has never been sent"
    )
