"""The warm pool is retired — and the retirement is visible, not implied.

Three things this proves, each of which was broken or invisible at HEAD:

1. ``POST /sandboxes/claim`` never hands out a pre-booted box. It cold-creates,
   so the sandbox's user and organization are in the container environment from
   boot (THE REQUEST CONTEXT IS CARRIED, NEVER REBUILT). Before this change the
   claim path *looked* alive while being unreachable — ``organization_id`` is
   required on every create request, and any request carrying one skipped the
   pool — so ``pool_loop`` booted and retired warm containers every 30 seconds
   for nobody.
2. Nothing in the orchestrator can pre-boot a sandbox any more: no warm
   container constructor, no claim, no loop.
3. Retirement cleans up and screams: leftover UNCLAIMED warm containers are
   removed, a claimed box is never touched, and a fleet setting still asking
   for warm boxes is named in the log instead of silently doing nothing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from orchestrator import pool
from orchestrator.store import InMemorySandboxStore
from tests.conftest import seed_store_sandbox_knobs

ORG_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "00000000-0000-0000-0000-0000000000aa"

POOL_SOURCE = Path(__file__).resolve().parents[1] / "orchestrator" / "pool.py"
ROUTES_SOURCE = Path(__file__).resolve().parents[1] / "orchestrator" / "routes" / "sandboxes.py"
MAIN_SOURCE = Path(__file__).resolve().parents[1] / "orchestrator" / "main.py"


class _FakeContainer:
    def __init__(self, sid: str, *, status: str = "running") -> None:
        self.id = f"cid-{sid}"
        self.status = status
        self.removed = False
        self.attrs = {
            "Config": {"Labels": {"matrx.sandbox_id": sid, pool.WARM_LABEL: "1",
                                  "matrx.template": "slim"}},
            "NetworkSettings": {"Ports": {}},
        }

    def remove(self, force: bool = False) -> None:
        self.removed = True


def test_no_code_path_can_pre_boot_a_sandbox() -> None:
    """The constructor, the claim and the loop are gone — not disabled."""
    source = POOL_SOURCE.read_text(encoding="utf-8")

    assert "containers.run(" not in source
    assert "def claim_warm" not in source
    assert "def pool_loop" not in source
    assert "def ensure_warm_pool" not in source
    assert "def retire_warm_pool" in source

    assert "pool_loop" not in MAIN_SOURCE.read_text(encoding="utf-8")


def test_claim_route_cold_creates_with_the_full_identity(monkeypatch) -> None:
    """The endpoint still answers; what it returns is a real create.

    A warm box would have arrived with the sentinel user and no organization
    in its environment — this asserts the create actually receives the
    caller's organization, which is the whole reason the pool cannot come
    back in its old shape.
    """
    from orchestrator.models import CreateSandboxRequest
    from orchestrator.routes import sandboxes as routes

    seen: dict[str, object] = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return "created"

    monkeypatch.setattr(routes.sandbox_manager, "create_sandbox", fake_create)
    monkeypatch.setattr(routes.settings, "host_tier", "hosted", raising=False)

    req = CreateSandboxRequest(user_id=USER_ID, organization_id=ORG_ID, template="slim")
    result = asyncio.run(routes.claim_sandbox(req))

    assert result == "created"
    assert seen["organization_id"] == ORG_ID
    assert seen["user_id"] == USER_ID


def test_claim_route_never_consults_a_pool() -> None:
    """No import of a claim helper survives in the route module."""
    source = ROUTES_SOURCE.read_text(encoding="utf-8")

    assert "claim_warm" not in source
    assert "_warm_template" not in source


def test_retirement_removes_unclaimed_warm_boxes_and_keeps_claimed_ones(monkeypatch) -> None:
    store = InMemorySandboxStore()
    seed_store_sandbox_knobs(store)

    unclaimed = _FakeContainer("sbx-warm00000001")
    claimed = _FakeContainer("sbx-warm00000002")

    from orchestrator.models import SandboxResponse, SandboxStatus
    from datetime import datetime, timezone

    asyncio.run(store.save(SandboxResponse(
        sandbox_id="sbx-warm00000002",
        user_id=USER_ID,
        organization_id=ORG_ID,
        status=SandboxStatus.READY,
        created_at=datetime.now(timezone.utc),
        hot_path="/home/agent",
        cold_path="/data/cold",
    )))

    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: store)
    monkeypatch.setattr(pool, "list_warm_containers", lambda template=None: [unclaimed, claimed])

    summary = asyncio.run(pool.retire_warm_pool())

    assert unclaimed.removed is True
    assert claimed.removed is False
    assert summary["removed"] == ["sbx-warm00000001"]
    assert summary["kept_claimed"] == 1


def test_a_settings_row_still_asking_for_warm_boxes_is_named_out_loud(
    monkeypatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A knob that no longer does anything must never look like it does."""
    store = InMemorySandboxStore()
    seed_store_sandbox_knobs(store)  # ships warm_pool_size=2

    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: store)
    monkeypatch.setattr(pool, "list_warm_containers", lambda template=None: [])

    with caplog.at_level(logging.WARNING, logger="orchestrator.pool"):
        summary = asyncio.run(pool.retire_warm_pool())

    assert summary["configured_target"] is not None
    text = caplog.text
    assert "WARM POOL SETTINGS IGNORED" in text
    assert "warm_pool_size=2" in text
    assert "RETIRED" in text


def test_store_failure_never_removes_a_box_that_might_be_owned(monkeypatch) -> None:
    """Fail safe: an unreadable store means "leave it alone", not "delete it"."""
    box = _FakeContainer("sbx-warm00000003")

    class _AngryStore:
        async def get(self, _sid):
            raise RuntimeError("store unavailable")

    monkeypatch.setattr("orchestrator.sandbox_manager._get_store", lambda: _AngryStore())
    monkeypatch.setattr(pool, "list_warm_containers", lambda template=None: [box])

    async def _no_knobs(*_a, **_k):
        raise RuntimeError("knobs unavailable")

    monkeypatch.setattr(pool, "knob_int", _no_knobs)
    monkeypatch.setattr(pool, "knob_str", _no_knobs)

    summary = asyncio.run(pool.retire_warm_pool())

    assert box.removed is False
    assert summary["removed"] == []
