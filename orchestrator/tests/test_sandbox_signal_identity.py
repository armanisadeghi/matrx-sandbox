"""A lifecycle signal must come from the sandbox it claims to be.

``POST /sandboxes/{id}/heartbeat|complete|error`` were identified by the
sandbox id in the path and nothing else, so any caller that could reach the
orchestrator with an id could end somebody else's session. The image's SDK now
forwards the acting user and the organization through its one header builder,
and these routes check them against the sandbox's own row.

The live fleet runs an image that sends neither header: absence is accepted and
REPORTED as unverified (never counted as a match), so the rollout does not take
226 boxes' heartbeats down. Anything else — a mismatch, or half an identity —
is refused with the remedy named, because a log line is not a check.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from orchestrator.main import app

USER = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
OTHER_ORG = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def manager():
    with patch("orchestrator.routes.sandboxes.sandbox_manager") as mock:
        mock.get_sandbox = AsyncMock(
            return_value=SimpleNamespace(
                sandbox_id="sbx-abcabcabcabc", user_id=USER, organization_id=ORG
            )
        )
        mock.heartbeat = AsyncMock(return_value=True)
        mock.destroy_sandbox = AsyncMock(return_value=True)
        yield mock


async def _post(path: str, headers: dict[str, str] | None = None, json=None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, headers=headers or {}, json=json)


@pytest.mark.asyncio
async def test_a_matching_identity_is_accepted(manager):
    response = await _post(
        "/sandboxes/sbx-abcabcabcabc/heartbeat",
        {"X-Matrx-User-Id": USER, "X-Organization-Id": ORG},
    )

    assert response.status_code == 200, response.text
    manager.heartbeat.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Matrx-User-Id": USER, "X-Organization-Id": OTHER_ORG},
        {"X-Matrx-User-Id": "99999999-9999-4999-8999-999999999999", "X-Organization-Id": ORG},
    ],
    ids=["wrong-organization", "wrong-user"],
)
async def test_a_mismatched_identity_is_refused_not_logged(manager, headers):
    response = await _post("/sandboxes/sbx-abcabcabcabc/heartbeat", headers)

    assert response.status_code == 403, response.text
    assert "not the identity this sandbox was created with" in response.json()["detail"]
    manager.heartbeat.assert_not_awaited()


@pytest.mark.asyncio
async def test_half_an_identity_is_refused_by_name(manager):
    response = await _post(
        "/sandboxes/sbx-abcabcabcabc/heartbeat", {"X-Matrx-User-Id": USER}
    )

    assert response.status_code == 403
    assert "X-Organization-Id" in response.json()["detail"]
    assert "provisioning defect" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_old_image_that_sends_nothing_still_works(manager):
    """The 226 running boxes predate the contract; their heartbeats survive."""
    response = await _post("/sandboxes/sbx-abcabcabcabc/heartbeat")

    assert response.status_code == 200, response.text
    manager.heartbeat.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_refuses_a_mismatch_before_destroying_anything(manager):
    response = await _post(
        "/sandboxes/sbx-abcabcabcabc/complete",
        {"X-Matrx-User-Id": USER, "X-Organization-Id": OTHER_ORG},
        json={"result": {}},
    )

    assert response.status_code == 403
    manager.destroy_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_refuses_a_mismatch_before_destroying_anything(manager):
    response = await _post(
        "/sandboxes/sbx-abcabcabcabc/error",
        {"X-Matrx-User-Id": USER, "X-Organization-Id": OTHER_ORG},
        json={"error": "boom"},
    )

    assert response.status_code == 403
    manager.destroy_sandbox.assert_not_awaited()
