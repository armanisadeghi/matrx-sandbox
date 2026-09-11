"""Durable startup serves existing rows before the expensive Docker census."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.main import _reconcile_boot_state


@pytest.mark.asyncio
async def test_boot_reconcile_runs_the_full_repair_pass_for_the_given_store() -> None:
    store = object()
    docker_summary = {"reconciled": 215, "scanned": 225, "skipped": 10, "failed": 0}
    liveness_summary = {"stopped": [], "refreshed": 215}

    with (
        patch(
            "orchestrator.reconcile.reconcile_from_docker",
            new=AsyncMock(return_value=docker_summary),
        ) as docker,
        patch(
            "orchestrator.reconcile.reconcile_liveness",
            new=AsyncMock(return_value=liveness_summary),
        ) as liveness,
    ):
        await _reconcile_boot_state(store)

    docker.assert_awaited_once_with(store)
    liveness.assert_awaited_once_with(store)
