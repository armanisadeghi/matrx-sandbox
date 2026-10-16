"""A Cloud Computer row in the shared ``sandbox_instances`` table never breaks the
orchestrator, and no orchestrator can be asked to spawn one.

AI Dream writes ``tier='cloud_computer'`` rows (a person's Fargate desktop). Every
orchestrator ``store.list()`` parses every row, and ``store.list()`` feeds the
reaper, liveness reconcile and always-on loops: one unparseable row would stop all
of them for both tiers. Fails before the stored-tier widening (pydantic refuses the
literal), passes after.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from orchestrator.models import CreateSandboxRequest
from orchestrator.store import _row_to_sandbox


def _row(tier: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": uuid.uuid4(),
        "sandbox_id": f"cc-{uuid.uuid4().hex}",
        "user_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "status": "stopped",
        "container_id": None,
        "created_at": now,
        "updated_at": now,
        "config": {},
        "labels": {"cloud_computer": "true"},
        "tier": tier,
    }


def test_a_cloud_computer_row_parses() -> None:
    sandbox = _row_to_sandbox(_row("cloud_computer"))
    assert sandbox.tier == "cloud_computer"


def test_an_unknown_tier_still_refuses() -> None:
    with pytest.raises(ValidationError):
        _row_to_sandbox(_row("mainframe"))


def test_no_orchestrator_can_be_asked_to_create_one() -> None:
    with pytest.raises(ValidationError):
        CreateSandboxRequest(user_id=str(uuid.uuid4()), tier="cloud_computer")
