"""Forcing checks for the boot-readiness class: a clock must never kill a box.

The defect these guard (2026-09-15, sbx-d374bfeef3dd and every later EC2
create for admin@admin.com): ``_wait_for_ready`` waited a hardcoded 120
seconds for ``/tmp/.sandbox_ready``. An 8,630-file S3 home restore takes
longer than that, so the orchestrator marked a healthy, still-booting box
``failed`` — and the dead row went on holding one of the user's five
admission slots, so the next create was refused too.

Each test below names the break it catches. Run them against the pre-fix
``_wait_for_ready`` and the first three fail: the fake container in
:class:`SlowHomeSyncContainer` never produces the ready marker inside 120
polled seconds.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator import boot_readiness as br
from orchestrator import sandbox_manager
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore

USER = "00000000-0000-4000-8000-000000000001"
ORG = "00000000-0000-4000-8000-000000000002"

#: What the real admin@admin.com home cost: 8,630 files. At the ~2s poll the
#: old code used, this box does not finish inside 120 seconds by any margin.
ADMIN_HOME_FILES = 8630


def row(sandbox_id: str = "sbx-guard") -> SandboxResponse:
    return SandboxResponse(
        sandbox_id=sandbox_id, user_id=USER, organization_id=ORG,
        status=SandboxStatus.STARTING, created_at=datetime.now(timezone.utc),
    )


class FakeContainer:
    """A container that answers the boot probe with a scripted phase script."""

    def __init__(self, script: list[dict], status: str = "running") -> None:
        self._script = script
        self._calls = 0
        self.status = status

    def exec_run(self, cmd):  # noqa: D401 - docker API shape
        step = self._script[min(self._calls, len(self._script) - 1)]
        self._calls += 1
        payload = (
            f"phase={step.get('phase', '')}\n"
            f"progress={step.get('progress', '')}\n"
            f"ready={'yes' if step.get('ready') else 'no'}\n"
            f"sdk={'yes' if step.get('sdk') else 'no'}\n"
        )
        return 0, payload.encode()


def install(monkeypatch, container, *, sleeps: list[float] | None = None):
    """Wire a fake docker client and a clock that advances only when we sleep."""
    clock = {"now": 0.0}
    recorded = sleeps if sleeps is not None else []

    class Client:
        class containers:  # noqa: N801 - docker API shape
            @staticmethod
            def get(_name):
                return container

    async def fake_sleep(seconds):
        recorded.append(seconds)
        clock["now"] += seconds

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(sandbox_manager, "_get_docker_client", lambda: Client)
    monkeypatch.setattr(sandbox_manager.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sandbox_manager.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(sandbox_manager.time, "monotonic", lambda: clock["now"])
    return clock


def seeded_store(monkeypatch=None, **overrides) -> InMemorySandboxStore:
    store = InMemorySandboxStore()
    values = {
        "active_sandbox_capacity": 5,
        "ready_timeout_seconds": 900,
        "home_sync_timeout_seconds": 3600,
    }
    values.update(overrides)
    store.seed_feature_knobs("infrastructure.sandbox", values)
    from orchestrator import knobs
    knobs.clear_knob_cache()
    if monkeypatch is not None:
        # knobs.py reads through the process-wide store, so the budgets only
        # come from THIS store if it is the one the module hands out.
        monkeypatch.setattr(sandbox_manager, "_get_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_big_home_sync_reaches_ready_instead_of_being_killed(monkeypatch):
    """Break caught: a 120s wall clock fails a box that is copying fine.

    The sync moves one batch of files per poll and takes far longer than the
    old hardcoded budget. It must end READY, not FAILED.
    """
    script = [
        {"phase": br.PHASE_CONTAINER},
        *[
            {"phase": br.PHASE_HOME_SYNC, "progress": f"{done}/{ADMIN_HOME_FILES}"}
            for done in range(25, ADMIN_HOME_FILES, 25)
        ],
        {"phase": br.PHASE_SDK, "sdk": True},
        {"phase": br.PHASE_READY, "sdk": True, "ready": True},
    ]
    container = FakeContainer(script)
    sleeps: list[float] = []
    install(monkeypatch, container, sleeps=sleeps)
    store = seeded_store(monkeypatch)

    result = await sandbox_manager._wait_for_ready(row(), store=store, poll_interval=2.0)

    assert result.status is SandboxStatus.READY
    # The proof that the old wall clock could not have survived this boot.
    assert sum(sleeps) > 120, f"boot finished in {sum(sleeps)}s — not the failing case"


@pytest.mark.asyncio
async def test_box_is_usable_while_its_home_is_still_syncing(monkeypatch):
    """Break caught: a box whose SDK answers is held hostage by its home sync.

    It must come back READY mid-sync AND carry the honest briefing line — an
    early hand-over with no explanation would be the silent half of the bug.
    """
    container = FakeContainer([
        {"phase": br.PHASE_HOME_SYNC, "progress": "10/8630"},
        {"phase": br.PHASE_HOME_SYNC, "progress": "4210/8630", "sdk": True},
    ])
    install(monkeypatch, container)
    store = seeded_store(monkeypatch)

    result = await sandbox_manager._wait_for_ready(row(), store=store, poll_interval=2.0)

    assert result.status is SandboxStatus.READY
    assert result.boot is not None
    assert result.boot.phase == br.PHASE_HOME_SYNC
    assert result.boot.files_done == 4210 and result.boot.files_total == 8630
    assert result.boot.briefing == "home sync in progress: 4210/8630 files"


@pytest.mark.asyncio
async def test_a_wedged_phase_still_fails_and_names_phase_and_count(monkeypatch):
    """Break caught: making the wait generous turns a dead box into a hang.

    A sync that stops moving must die on its own phase budget, and the reason
    must name the phase and the count it reached — never a bare number.
    """
    container = FakeContainer([{"phase": br.PHASE_HOME_SYNC, "progress": "4210/8630"}])
    install(monkeypatch, container)
    store = seeded_store(monkeypatch, home_sync_timeout_seconds=30)

    result = await sandbox_manager._wait_for_ready(row(), store=store, poll_interval=2.0)

    assert result.status is SandboxStatus.FAILED
    assert br.PHASE_HOME_SYNC in result.stop_reason
    assert "4210/8630 files" in result.stop_reason
    assert "home_sync_timeout_seconds" in result.stop_reason


@pytest.mark.asyncio
async def test_a_pre_marker_image_is_judged_by_the_operator_budget(monkeypatch):
    """Break caught: the fix only works on boxes built from the new image.

    Every container running in the fleet today predates the phase files. Such
    a box reports no phase and must still get the generous knob budget and the
    same readiness signals — not a resurrected 120.
    """
    container = FakeContainer([
        {},                       # old image: no phase, no progress
        {"ready": True},
    ])
    install(monkeypatch, container)
    store = seeded_store(monkeypatch)

    result = await sandbox_manager._wait_for_ready(row(), store=store, poll_interval=2.0)
    assert result.status is SandboxStatus.READY


@pytest.mark.asyncio
async def test_budget_comes_from_the_knob_and_nothing_else(monkeypatch):
    """Break caught: a constant creeps back in as a 'safety' floor or cap."""
    container = FakeContainer([{"phase": br.PHASE_ENVIRONMENT}])
    install(monkeypatch, container)
    store = seeded_store(monkeypatch, ready_timeout_seconds=10)
    sleeps: list[float] = []
    install(monkeypatch, container, sleeps=sleeps)

    result = await sandbox_manager._wait_for_ready(row(), store=store, poll_interval=2.0)
    assert result.status is SandboxStatus.FAILED
    # 10s budget honoured exactly: five 2s polls, not a hidden minimum.
    assert sum(sleeps) == pytest.approx(10.0)


def test_no_readiness_wait_carries_a_hardcoded_budget():
    """Census guard: the class, not the instance.

    The defect was not the number 120 — it was a readiness wait owning its own
    budget. Sweep every ``_wait_*`` / ``*_ready`` function in the orchestrator
    package and refuse any numeric default on a timeout/budget/deadline
    parameter. Budgets reach these functions from knobs or from an explicit
    caller-computed deadline, never from a literal.
    """
    package = Path(sandbox_manager.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if not (name.startswith("_wait") or name.endswith("_ready")
                    or "readiness" in name):
                continue
            args = node.args
            params = args.args + args.posonlyargs + args.kwonlyargs
            defaults = list(args.defaults) + [d for d in args.kw_defaults if d]
            named = {p.arg for p in params}
            for default in defaults:
                if not isinstance(default, ast.Constant) or not isinstance(
                    default.value, (int, float)
                ):
                    continue
                if any(
                    key in arg
                    for arg in named
                    for key in ("timeout", "budget", "deadline")
                ):
                    offenders.append(f"{path.name}:{name} (default {default.value})")
                    break
    assert not offenders, (
        "readiness waits carrying their own hardcoded budget: " + ", ".join(offenders)
    )


def test_wait_for_ready_takes_no_caller_timeout():
    """Break caught: the 120 comes back as a default nobody passes."""
    signature = inspect.signature(sandbox_manager._wait_for_ready)
    for name in signature.parameters:
        assert "timeout" not in name and "budget" not in name, (
            f"_wait_for_ready takes a caller-supplied {name}; budgets are knobs"
        )
    # And the budgets it does use come from the knob layer, not from itself.
    body = inspect.getsource(sandbox_manager._wait_for_ready)
    body = body.split('"""', 2)[-1]  # ignore the docstring's account of the bug
    assert 'knob_int("ready_timeout_seconds")' in body
    assert 'knob_int("home_sync_timeout_seconds")' in body
    assert "120" not in body


def test_every_entrypoint_publishes_its_phases():
    """Census guard: a tier whose image stays silent is the old bug, scoped.

    Readiness is only a phase signal if every entrypoint actually sends one.
    """
    repo = Path(sandbox_manager.__file__).resolve().parents[2]
    entrypoints = [
        repo / "sandbox-image/scripts/entrypoint.sh",
        repo / "sandbox-image/scripts/entrypoint-slim.sh",
        repo / "sandbox-image/scripts/entrypoint-aidream.sh",
        repo / "sandbox-local/scripts/entrypoint-local.sh",
    ]
    missing = [
        str(path) for path in entrypoints
        if path.exists() and "boot-phase.sh" not in path.read_text()
    ]
    assert not missing, f"entrypoints that never report a boot phase: {missing}"
    assert (repo / "sandbox-image/scripts/boot-phase.sh").exists()


def test_hot_sync_publishes_file_counts():
    """Census guard: the longest phase must be the one that reports movement."""
    repo = Path(sandbox_manager.__file__).resolve().parents[2]
    hot_sync = (repo / "sandbox-image/scripts/hot-sync.sh").read_text()
    assert "boot-phase.sh home_sync" in hot_sync
    assert "count_remote_objects" in hot_sync


def test_a_failed_boot_can_actually_store_its_reason():
    """Break caught: the reason is computed, logged, and then dropped.

    ``_wait_for_ready`` now names the phase and count it died in. That is only
    the "nothing fails silently" fix if the durable row can hold it: the save
    upsert used to keep ONLY the stop_reason already stored, so a create that
    failed on readiness left a `failed` row with a blank explanation.
    """
    from orchestrator import store as store_module

    source = Path(store_module.__file__).read_text()
    # The save() upsert is the one that resolves a conflict on sandbox_id.
    conflict = source.index("ON CONFLICT (sandbox_id) DO UPDATE")
    start = source.rindex("INSERT INTO sandbox_instances", 0, conflict)
    save_sql = source[start : source.index("RETURNING id", conflict)]
    assert "stop_reason" in save_sql.split("VALUES")[0], (
        "save() cannot write a stop_reason at all"
    )
    assert "COALESCE(EXCLUDED.stop_reason" in save_sql, (
        "save() discards a caller-computed stop_reason on update"
    )


@pytest.mark.asyncio
async def test_a_refusal_names_the_boxes_and_how_to_free_one():
    """Break caught: the ceiling refuses with a number and no way forward."""
    from orchestrator.store import AdmissionCapacityExceeded

    store = InMemorySandboxStore()
    store.seed_feature_knobs("infrastructure.sandbox", {"active_sandbox_capacity": 2})
    for sid in ("sbx-aug-one", "sbx-aug-two"):
        await store.reserve_active(row(sid))

    with pytest.raises(AdmissionCapacityExceeded) as refused:
        await store.reserve_active(row("sbx-new"))

    message = refused.value.remedy()
    assert "sbx-aug-one" in message and "sbx-aug-two" in message
    assert "Stop or delete one of them" in message
    assert "active_sandbox_capacity" in message
    assert {o["sandbox_id"] for o in refused.value.occupants} == {
        "sbx-aug-one", "sbx-aug-two",
    }
