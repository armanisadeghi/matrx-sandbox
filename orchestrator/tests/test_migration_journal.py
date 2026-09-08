import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore


@pytest.mark.asyncio
async def test_journal_claim_is_exclusive_and_fences_lifecycle_and_stale_save():
    store = InMemorySandboxStore()
    source = SandboxResponse(
        sandbox_id="sbx-journal", user_id="00000000-0000-0000-0000-000000000001",
        organization_id="00000000-0000-0000-0000-000000000002",
        status=SandboxStatus.RUNNING, container_id="source-id",
        created_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc)-timedelta(seconds=1),
        tier="ec2", template="slim",
    )
    await store.save(source)
    claims = await asyncio.gather(*[
        store.claim_migration(source.sandbox_id, op_id=op, source_container_id="source-id", source_image="old", target_image="new")
        for op in ("operation-a", "operation-b")
    ])
    assert sum(bool(claim) for claim in claims) == 1
    journal = next(claim for claim in claims if claim)
    assert await store.expire_stale() == []
    assert not await store.update_status(source.sandbox_id, SandboxStatus.STOPPED)
    assert not await store.delete(source.sandbox_id)
    assert not await store.soft_delete(source.sandbox_id)
    with pytest.raises(RuntimeError, match="migration"):
        await store.save(source)
    assert not await store.advance_migration(source.sandbox_id, op_id="wrong", expected_phase="claimed", phase="verified", patch={})
    assert await store.advance_migration(source.sandbox_id, op_id=journal["op_id"], expected_phase="claimed", phase="verified", patch={"candidate_id":"candidate-id"})
    assert await store.commit_migration(source.sandbox_id, op_id=journal["op_id"], expected_phase="verified", candidate_id="candidate-id", target_version="new-version")
    persisted = await store.get(source.sandbox_id)
    assert persisted.container_id == "candidate-id"
    assert persisted.config["_migration"]["phase"] == "committed"
    with pytest.raises(RuntimeError, match="migration|stale"):
        await store.save(source)
