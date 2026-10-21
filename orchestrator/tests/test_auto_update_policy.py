"""SBX-013 automatic-update policy gates (orchestrator/auto_update_policy.py)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator import auto_update_policy as aup

NOW = 1_800_000_000.0

# Real `ps -eo user=,pid=,ppid=,args=` from an idle EC2 development box.
IDLE_PS = """\
root           1       0 tini -- /opt/sandbox/scripts/entrypoint-slim.sh
root           6       1 bash /opt/sandbox/scripts/entrypoint-slim.sh
root           7       6 bash /opt/sandbox/scripts/entrypoint-slim.sh
root           9       7 tee -a /var/log/sandbox/entrypoint.log
root          38       1 sshd: /usr/sbin/sshd [listener] 0 of 10-100 startups
agent    2542376       1 bash -c cd /home/agent && PYTHONDONTWRITEBYTECODE=1 python3 -m uvicorn matrx_agent.api.main:app --host 0.0.0.0 --port 8000 >> /var/log/sandbox/api.log 2>&1 &
agent    2542377 2542376 python3 -m uvicorn matrx_agent.api.main:app --host 0.0.0.0 --port 8000
root     2557811       6 sleep 10
root     2557999       0 ps -eo user=,pid=,ppid=,args=
"""


class FakeContainer:
    def __init__(self, *, labels=None, mounts=None, ps=IDLE_PS, opt_out_code=1,
                 ps_code=0):
        self.labels = labels if labels is not None else {
            "matrx.tier": "hosted", "matrx.template": "slim"}
        self.attrs = {"Mounts": mounts if mounts is not None else [
            {"Type": "volume", "Destination": "/home/agent", "RW": True}]}
        self.ps, self.opt_out_code, self.ps_code = ps, opt_out_code, ps_code
        self.calls = []

    def exec_run(self, cmd, user="root", demux=False):
        self.calls.append((tuple(cmd), user))
        if cmd[0] == "test":
            return SimpleNamespace(exit_code=self.opt_out_code, output=b"")
        if cmd[0] == "ps":
            return SimpleNamespace(exit_code=self.ps_code, output=self.ps.encode())
        raise AssertionError(f"unexpected exec {cmd}")


def box(sid="sbx-a", version="v2"):
    return SimpleNamespace(sandbox_id=sid, current_version=version,
                           current_image_id=f"sha256:{version}", drifted=True)


def ledger(**targets):
    return {"version": 1, "targets": dict(targets), "boxes": {}}


def settled(**extra):
    return {"first_seen": NOW - 10 * 3600, "canary_passes": [], "halted": None, **extra}


# ---------------------------------------------------------------- policy ----

def _raw_from(values):
    async def raw(feature, key):
        assert feature == "infrastructure.sandbox"
        if key not in values:
            raise RuntimeError(f"knob {key} has no row")
        return values[key]
    return raw


@pytest.mark.asyncio
async def test_missing_stage_knob_means_off():
    policy = await aup.resolve_policy(_raw_from({}))
    assert policy.stage == "off"
    assert policy.settle_seconds == 3600 and policy.max_failures_per_box == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["on", "FLEET", 1, True, None, ["fleet"]])
async def test_unknown_stage_values_fail_closed(stage):
    policy = await aup.resolve_policy(_raw_from({"auto_update_stage": stage}))
    assert policy.stage == "off"
    assert policy.notes


@pytest.mark.asyncio
async def test_stage_and_canaries_read_once():
    policy = await aup.resolve_policy(_raw_from({
        "auto_update_stage": "canary",
        "auto_update_canary_sandbox_ids": "sbx-a, sbx-b",
        "auto_update_max_failures_per_box": 9,   # cannot loosen past default
        "auto_update_settle_seconds": "soon",    # invalid -> default
    }))
    assert policy.stage == "canary"
    assert policy.canary_ids == ("sbx-a", "sbx-b")
    assert policy.max_failures_per_box == 2
    assert policy.settle_seconds == 3600


@pytest.mark.asyncio
async def test_stage_off_never_reaches_docker_even_with_master_switch(monkeypatch):
    """auto_migrate=true plus a valid quiet interval still moves nothing."""
    from orchestrator import knobs, migrate

    monkeypatch.setattr(knobs, "_raw", AsyncMock(side_effect=lambda f, k: (
        1800 if k == "auto_update_idle_seconds" else (_ for _ in ()).throw(RuntimeError("no row")))))
    docker = Mock(side_effect=AssertionError("stage off reached Docker"))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", docker)
    migrate_one = AsyncMock(side_effect=AssertionError("stage off migrated"))
    monkeypatch.setattr(migrate, "migrate_sandbox", migrate_one)

    result = await migrate.migrate_all_drifted(store=object())

    assert result["auto_update"]["stage"] == "off"
    assert "error" not in result
    docker.assert_not_called()
    migrate_one.assert_not_called()


# --------------------------------------------------------------- storage ----

def test_storage_classes():
    assert aup.storage_class(FakeContainer()) == "named_volume"
    bind = FakeContainer(mounts=[{"Type": "bind", "Destination": "/home/agent",
                                  "Source": "/mnt/matrx-internal-development/primary"}],
                         labels={"matrx.tier": "ec2", "matrx.template": "development"})
    assert aup.storage_class(bind) == "host_bind"
    slim = FakeContainer(mounts=[], labels={"matrx.tier": "ec2", "matrx.template": "slim"})
    assert aup.storage_class(slim) == "ec2_writable_layer"
    dev_layer = FakeContainer(mounts=[], labels={"matrx.tier": "ec2", "matrx.template": "development"})
    assert aup.storage_class(dev_layer) == "unknown"


# --------------------------------------------------------------- probes -----

def test_idle_baseline_has_no_detached_work():
    assert aup.detached_processes(IDLE_PS) == []


def test_agent_processes_are_detached_work():
    ps = IDLE_PS + (
        "agent      4000       1 node /home/agent/repos/app/node_modules/.bin/next dev\n"
        "agent      4001       1 sleep 120\n"
        "agent      4002       1 [python3] <defunct>\n"
    )
    offenders = aup.detached_processes(ps)
    assert len(offenders) == 2
    assert any("next dev" in o for o in offenders)
    assert any("sleep 120" in o for o in offenders)  # agent sleep is a running tool


@pytest.mark.asyncio
async def test_opt_out_label_and_file():
    assert await aup.opted_out(FakeContainer(labels={aup.OPT_OUT_LABEL: "off"}))
    assert await aup.opted_out(FakeContainer(opt_out_code=0))
    assert not await aup.opted_out(FakeContainer(opt_out_code=1))
    with pytest.raises(RuntimeError):
        await aup.opted_out(FakeContainer(opt_out_code=126))


# ---------------------------------------------------------------- admit -----

def fleet(**kw):
    return aup.Policy(stage="fleet", **kw)


@pytest.mark.asyncio
async def test_admit_gate_order_and_reasons():
    c = FakeContainer()
    assert (await aup.admit(c, box(), aup.Policy(), ledger(), NOW)).reason == "stage_off"
    assert (await aup.admit(c, box(), aup.Policy(stage="canary"), ledger(v2=settled()), NOW)
            ).reason == "not_a_canary"
    assert (await aup.admit(c, box(), fleet(), ledger(v2=settled()), NOW)
            ).reason == "awaiting_canary_soak"
    fresh_pass = settled(canary_passes=[{"sandbox_id": "sbx-c", "at": NOW - 60}])
    assert (await aup.admit(c, box(), fleet(), ledger(v2=fresh_pass), NOW)
            ).reason == "awaiting_canary_soak"
    soaked = settled(canary_passes=[{"sandbox_id": "sbx-c", "at": NOW - 5 * 3600}])
    d = await aup.admit(c, box(), fleet(), ledger(v2=soaked), NOW)
    assert d.admit and d.detail == {"target": "v2", "canary": False, "storage": "named_volume"}


@pytest.mark.asyncio
async def test_new_target_settles_before_anything_moves():
    led = ledger()
    canary = aup.Policy(stage="canary", canary_ids=("sbx-a",))
    d = await aup.admit(FakeContainer(), box(), canary, led, NOW)
    assert d.reason == "target_settling"
    assert led["targets"]["v2"]["first_seen"] == NOW
    d = await aup.admit(FakeContainer(), box(), canary, led, NOW + 3601)
    assert d.admit and d.detail["canary"] is True


@pytest.mark.asyncio
async def test_halt_hold_spacing_storage_optout_detached():
    canary = aup.Policy(stage="canary", canary_ids=("sbx-a",))
    led = ledger(v2=settled(halted={"reason": "x"}))
    assert (await aup.admit(FakeContainer(), box(), canary, led, NOW)).reason == "target_halted"

    led = ledger(v2=settled())
    led["boxes"]["sbx-a"] = {"held": {"reason": "2 failed attempts"}}
    assert (await aup.admit(FakeContainer(), box(), canary, led, NOW)).reason == "box_held"

    led["boxes"]["sbx-a"] = {"held": None, "failures": 1, "last_attempt_at": NOW - 60}
    assert (await aup.admit(FakeContainer(), box(), canary, led, NOW)).reason == "attempt_spacing"

    led["boxes"] = {}
    bind = FakeContainer(mounts=[{"Type": "bind", "Destination": "/home/agent"}])
    d = await aup.admit(bind, box(), canary, led, NOW)
    assert d.reason == "manual_only" and d.detail["storage"] == "host_bind"
    assert bind.calls == []  # no in-box probe for a box we would never move

    assert (await aup.admit(FakeContainer(opt_out_code=0), box(), canary, led, NOW)
            ).reason == "opted_out"
    busy = FakeContainer(ps=IDLE_PS + "agent 5 1 python3 train.py\n")
    d = await aup.admit(busy, box(), canary, led, NOW)
    assert d.reason == "detached_work" and "train.py" in d.detail["processes"][0]
    d = await aup.admit(FakeContainer(ps_code=1), box(), canary, led, NOW)
    assert d.reason == "probe_failed"


# --------------------------------------------------------------- outcomes ---

def _decision(sid="sbx-a", canary=False):
    return aup.Decision(sid, True, "admitted", {"target": "v2", "canary": canary})


def test_canary_success_records_pass_and_resets_failures():
    led = ledger(v2=settled())
    led["boxes"]["sbx-a"] = {"failures": 1, "held": None, "history": []}
    assert aup.record_outcome(led, _decision(canary=True), "migrated", now=NOW) == "info"
    assert led["targets"]["v2"]["canary_passes"] == [{"sandbox_id": "sbx-a", "at": NOW}]
    assert led["boxes"]["sbx-a"]["failures"] == 0


def test_deferral_is_not_an_attempt():
    led = ledger(v2=settled())
    assert aup.record_outcome(led, _decision(), "busy_deferred", now=NOW) == "info"
    assert "last_attempt_at" not in led["boxes"]["sbx-a"]


def test_preservation_mismatch_halts_target_and_holds_box():
    led = ledger(v2=settled())
    sev = aup.record_outcome(led, _decision(), "migrated", now=NOW,
                             problems=["file sha256 changed: notes.md"])
    assert sev == "error"
    assert led["targets"]["v2"]["halted"]["reason"] == "preservation_mismatch"
    assert led["boxes"]["sbx-a"]["held"]
    report = aup.fleet_report(led)
    assert "v2" in report["halted_targets"] and "sbx-a" in report["held_boxes"]


def test_canary_failure_halts_target_fleet_needs_two():
    led = ledger(v2=settled())
    aup.record_outcome(led, _decision("sbx-c", canary=True), "failed", now=NOW)
    assert led["targets"]["v2"]["halted"]

    led = ledger(v2=settled())
    aup.record_outcome(led, _decision("sbx-a"), "failed", now=NOW)
    assert not led["targets"]["v2"]["halted"]
    assert not led["boxes"]["sbx-a"]["held"]
    aup.record_outcome(led, _decision("sbx-b"), "failed", now=NOW)
    assert led["targets"]["v2"]["halted"]


def test_rollback_failed_holds_box_immediately():
    led = ledger(v2=settled())
    aup.record_outcome(led, _decision(), "rollback_failed", now=NOW)
    assert led["boxes"]["sbx-a"]["held"] and led["targets"]["v2"]["halted"]


# ------------------------------------------------------------- manifests ----

def test_compare_manifests():
    base = {"files": {"a.txt": {"sha256": "1", "mode": 33188, "uid": 1000, "gid": 1000}},
            "repos": {"repos/x": {"head": "h1", "dirty": True, "dirty_sha256": "d1", "stashes": 0}}}
    assert aup.compare_manifests(base, json.loads(json.dumps(base))) == ([], [])

    stashed = json.loads(json.dumps(base))
    stashed["repos"]["repos/x"].update(dirty=False, dirty_sha256="clean", stashes=1)
    assert aup.compare_manifests(base, stashed) == ([], ["uncommitted work autostashed: repos/x"])

    lost = json.loads(json.dumps(base))
    lost["repos"]["repos/x"].update(dirty=False, dirty_sha256="clean")
    lost["files"]["a.txt"]["uid"] = 0
    problems, _ = aup.compare_manifests(base, lost)
    assert problems == ["file uid changed: a.txt", "uncommitted work changed: repos/x"]

    moved = json.loads(json.dumps(base))
    moved["repos"]["repos/x"]["head"] = "h2"
    del moved["files"]["a.txt"]
    problems, _ = aup.compare_manifests(base, moved)
    assert problems == ["file missing after update: a.txt", "git HEAD moved: repos/x"]


def _run_probe(home: Path) -> dict:
    script = aup.PRESERVATION_PROBE.replace('HOME = "/home/agent"', f'HOME = {str(home)!r}')
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, check=True,
                         timeout=120).stdout
    return json.loads(out)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_probe_script_end_to_end(tmp_path):
    repo = tmp_path / "repos" / "app"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "main.py").write_text("print(1)\n")
    _git(repo, "add", "main.py")
    _git(repo, "commit", "-qm", "init")
    (repo / "main.py").write_text("print(2)\n")  # uncommitted work
    (tmp_path / "notes.md").write_text("keep me\n")
    (tmp_path / ".matrx" / "runtime").mkdir(parents=True)
    (tmp_path / ".matrx" / "runtime" / "live.log").write_text("noise\n")

    before = _run_probe(tmp_path)
    assert "notes.md" in before["files"]
    assert not any(p.startswith(".matrx/runtime") for p in before["files"])
    assert before["repos"]["repos/app"]["dirty"] is True

    (tmp_path / ".matrx" / "runtime" / "live.log").write_text("more noise\n")
    assert aup.compare_manifests(before, _run_probe(tmp_path)) == ([], [])

    _git(repo, "stash", "-q")  # what a boot-time autostash does
    problems, notes = aup.compare_manifests(before, _run_probe(tmp_path))
    assert problems == [] and notes == ["uncommitted work autostashed: repos/app"]

    _git(repo, "stash", "drop", "-q")  # work actually lost
    problems, _ = aup.compare_manifests(before, _run_probe(tmp_path))
    assert "uncommitted work changed: repos/app" in problems


# ---------------------------------------------------------------- ledger ----

def test_ledger_roundtrip_and_corruption(tmp_path):
    store = aup.Ledger(tmp_path, require_mount=False)
    data = store.load()
    assert data == {"version": 1, "targets": {}, "boxes": {}}
    aup.observe_target(data, "v2", NOW)
    store.save(data)
    assert store.load()["targets"]["v2"]["first_seen"] == NOW
    assert not list(tmp_path.glob("*.json"))  # never collides with journal glob
    store.path.write_text("{not json")
    with pytest.raises(aup.LedgerUnavailable):
        store.load()


def test_ledger_requires_durable_mount(tmp_path):
    with pytest.raises(aup.LedgerUnavailable):
        aup.Ledger(tmp_path).load()


def test_clear_hold(tmp_path):
    store = aup.Ledger(tmp_path, require_mount=False)
    data = ledger(v2=settled(halted={"reason": "x"}))
    data["boxes"]["sbx-a"] = {"failures": 2, "held": {"reason": "y"}}
    store.save(data)
    out = aup.clear_hold(store, "sbx-a", "v2")
    assert out["boxes"]["sbx-a"]["held"] is None and out["targets"]["v2"]["halted"] is None


# ------------------------------------------------------- migrate_all hook ---

@pytest.mark.asyncio
async def test_migrate_all_halts_fleet_on_preservation_mismatch(monkeypatch, tmp_path):
    from orchestrator import knobs, migrate

    import time

    store_ledger = aup.Ledger(tmp_path, require_mount=False)
    seed = ledger(v2=settled(first_seen=time.time() - 10 * 3600))
    store_ledger.save(seed)
    monkeypatch.setattr(aup, "Ledger", lambda: store_ledger)
    monkeypatch.setattr(knobs, "_raw", AsyncMock(side_effect=_raw_from({
        "auto_update_idle_seconds": 1800, "auto_update_stage": "canary",
        "auto_update_canary_sandbox_ids": ["sbx-a"]})))
    containers = {"sbx-a": FakeContainer(), "sbx-b": FakeContainer()}
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda sid: containers[sid]))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", lambda: client)
    monkeypatch.setattr("orchestrator.versioning.compute_drift",
                        lambda c: [box("sbx-a"), box("sbx-b")])
    manifests = iter([
        {"files": {"n.md": {"sha256": "1", "mode": 1, "uid": 1, "gid": 1}}, "repos": {}},
        {"files": {"n.md": {"sha256": "2", "mode": 1, "uid": 1, "gid": 1}}, "repos": {}},
    ])
    monkeypatch.setattr(aup, "preservation_manifest", AsyncMock(side_effect=lambda c: next(manifests)))
    migrate_one = AsyncMock(return_value={"status": "migrated"})
    monkeypatch.setattr(migrate, "migrate_sandbox", migrate_one)

    result = await migrate.migrate_all_drifted(store=object())

    assert result["failed"] == ["sbx-a"]
    assert result["deferred"] == ["sbx-b"]  # not a canary
    migrate_one.assert_awaited_once()
    persisted = store_ledger.load()
    assert persisted["targets"]["v2"]["halted"]["reason"] == "preservation_mismatch"
    assert "sbx-a" in result["auto_update"]["held_boxes"]
    reasons = {d["sandbox_id"]: d["reason"] for d in result["auto_update"]["decisions"]}
    # The halt applies within the same pass: sbx-b is stopped by it.
    assert reasons == {"sbx-a": "admitted", "sbx-b": "target_halted"}


@pytest.mark.asyncio
async def test_migrate_all_stops_without_durable_ledger(monkeypatch):
    from orchestrator import knobs, migrate

    monkeypatch.setattr(knobs, "_raw", AsyncMock(side_effect=_raw_from({
        "auto_update_idle_seconds": 1800, "auto_update_stage": "fleet"})))
    monkeypatch.setattr(aup, "Ledger", lambda: SimpleNamespace(
        load=Mock(side_effect=aup.LedgerUnavailable("not a durable mount"))))
    docker = Mock(side_effect=AssertionError("reached Docker without a ledger"))
    monkeypatch.setattr("orchestrator.sandbox_manager._get_docker_client", docker)

    result = await migrate.migrate_all_drifted(store=object())

    assert "ledger unavailable" in result["error"]
    docker.assert_not_called()


@pytest.mark.asyncio
async def test_status_block_never_raises_without_ledger_mount(monkeypatch):
    async def raw(feature, key):
        raise KeyError(key)

    monkeypatch.setattr("orchestrator.knobs._raw", raw)
    block = await aup.status_block()
    assert block["stage"] == "off"
    assert "ledger_error" in block  # no durable mount in the test runner
