"""Forcing checks for the active-slot admission boundary.

The production implementation uses a PostgreSQL transaction/advisory lock;
these tests exercise the identical store contract with independently scheduled
callers.  The isolated-Postgres two-pool proof is intentionally selected only
when a disposable database fixture is supplied by the integration harness.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import AdmissionCapacityExceeded, InMemorySandboxStore

USER = "00000000-0000-4000-8000-000000000001"
ORG = "00000000-0000-4000-8000-000000000002"


def row(sandbox_id: str, *, user: str = USER, org: str = ORG) -> SandboxResponse:
    return SandboxResponse(
        sandbox_id=sandbox_id, user_id=user, organization_id=org,
        status=SandboxStatus.CREATING, created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_competing_admissions_keep_exact_ceiling_and_preserve_refusal() -> None:
    """Break caught: count-before-insert lets two callers exceed one slot."""
    store = InMemorySandboxStore()
    store.seed_feature_knobs("infrastructure.sandbox", {"active_sandbox_capacity": 1})

    async def reserve(sid: str):
        try:
            await store.reserve_active(row(sid))
            return "admitted"
        except AdmissionCapacityExceeded as exc:
            return exc

    first, second = await asyncio.gather(reserve("sbx-hosted"), reserve("sbx-ec2"))
    assert sorted(type(item).__name__ if isinstance(item, Exception) else item for item in (first, second)) == [
        "AdmissionCapacityExceeded", "admitted"
    ]
    active = await store.list(user_id=USER)
    assert [item.sandbox_id for item in active] in (["sbx-hosted"], ["sbx-ec2"])


@pytest.mark.asyncio
async def test_replacement_excludes_only_callers_exact_active_predecessor() -> None:
    """Break caught: a forged predecessor can borrow another user's slot."""
    store = InMemorySandboxStore()
    store.seed_feature_knobs("infrastructure.sandbox", {"active_sandbox_capacity": 1})
    predecessor = row("sbx-old")
    await store.reserve_active(predecessor)

    await store.reserve_active(row("sbx-next"), replacement_for="sbx-old")
    assert {item.sandbox_id for item in await store.list(user_id=USER)} == {"sbx-old", "sbx-next"}

    with pytest.raises(RuntimeError, match="replacement predecessor"):
        await store.reserve_active(row("sbx-forged"), replacement_for="sbx-other")
