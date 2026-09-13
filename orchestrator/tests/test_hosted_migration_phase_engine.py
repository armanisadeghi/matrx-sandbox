from orchestrator.hosted_migration import HostedMigrationJournal, _mountinfo_has_mountpoint, recovery_action, transition, HostedMigrationStateError, validate_record
from orchestrator.hosted_migration import hosted_fenced, hosted_volume_fenced
from orchestrator.hosted_runtime import HostedMigrationBusyError, _assert_migration_home_exclusive, _assert_no_unowned_home_writer, _migration_failure_status, _pause_target_for_rollback, _quiesce_target, _recover_pre_copy_source, _remove_verified_pre_copy_helper, _require_paused_source, _require_rollback_safe_target
from orchestrator.hosted_runtime import _activate_promoted_target, _ensure_rollback_home, _record_error, _wait_migration_state, recover_hosted_migration
from orchestrator.migrate import _wait_container_ready
from orchestrator.models import SandboxResponse, SandboxStatus
from orchestrator.store import InMemorySandboxStore
from datetime import datetime, timezone
from pathlib import Path
import pytest
import asyncio


def _base():
    return {
        "schema_version": 1, "sandbox_id": "sbx", "old_id": "old", "old_name": "sbx",
        "old_image": "sha256:" + "a" * 64, "source_volume": "home",
        "source_identity": {"name": "home"}, "row_identity": {"sandbox_id": "sbx"},
        "target_name": "sbx-mig-op", "target_image": "sha256:" + "b" * 64,
        "operation_label": "op", "backup_name": "backup", "helper_image": "sha256:" + "c" * 64,
        "rollback_name": "sbx-old-op", "verify_timeout": 1, "stop_timeout": 1,
        "source_endpoint": {"network": "bridge", "network_id": "network-id", "aliases": ["sbx"], "requested_aliases": ["sbx"],
                            "ipv4_address": "172.17.0.2", "ipv4_address_prefixlen": 16, "mac_address": "02:42:ac:11:00:02"},
        "network_disconnect_receipt": {"old_id": "old", "network": "bridge", "network_id": "network-id", "absent": True},
        "pre_cas_home_receipt": {"manifest_sha256": "digest", "source_volume": {"name": "home"}},
        "activation_home_preflight_receipt": {"target_id": "new", "agent_lifecycle_paths_writable": True},
    }


@pytest.mark.parametrize("phase", ["admitted", "old_stopped", "backup_intent"])
def test_prebackup_crash_resumes_the_original_paused_process_without_restore(phase):
    """Break caught: pre-copy crash left an unchanged source permanently paused or restarted it."""
    record = _base(); record.pop("network_disconnect_receipt")
    assert recovery_action(dict(record, phase=phase), db_container_id="old", target_exists_ready=False) == "resume_pre_copy_source"


@pytest.mark.asyncio
async def test_old_stopped_recovery_unpauses_exact_original_pid_without_starting_it():
    """Break caught: pause-before-backup crash either stranded home or booted the old entrypoint."""
    from docker.errors import NotFound
    record = dict(_base(), phase="old_stopped", old_process_identity={"pid": 4242, "started_at": "2026-09-12T00:00:00Z"})
    record["row_identity"] = {"sandbox_id": "sbx", "user_id": "", "organization_id": "", "created_at": ""}
    old = _Container("old", status="paused", writable_home=True)
    old.name = "/sbx"
    old.attrs.update({"Image": record["old_image"], "State": {"Pid": 4242, "StartedAt": "2026-09-12T00:00:00Z"},
                      "NetworkSettings": {"Networks": {"bridge": {"Aliases": ["sbx"], "IPAddress": "172.17.0.2/16", "IPPrefixLen": 16, "GlobalIPv6Address": "", "MacAddress": "changed"}}}})
    old.start_calls = 0
    old.start = lambda: setattr(old, "start_calls", old.start_calls + 1)
    class Containers:
        def get(self, identity):
            if identity == "old": return old
            raise NotFound("container", identity)
        def list(self, **_kwargs): return [old]
    client = type("Client", (), {"containers": Containers(), "networks": type("Networks", (), {"get": lambda *_: type("Network", (), {"id": "network-id"})()})()})()
    row = type("Row", (), {"sandbox_id": "sbx", "user_id": "", "organization_id": "", "created_at": "", "container_id": "old"})()
    class Store:
        async def get(self, *_): return row
        async def get_lifecycle(self, *_): return {"status": "ready"}
    resumed = await _recover_pre_copy_source(record, store=Store(), client=client,
                                              journal=type("Journal", (), {"write": lambda *_: None})())
    assert resumed is old and old.unpause_calls == 1 and old.start_calls == 0


@pytest.mark.asyncio
async def test_backup_intent_accepts_exact_helper_with_inherited_oci_labels():
    """Break caught: backup_intent could strand a paused source after a valid helper crash."""
    from docker.errors import NotFound
    from orchestrator.hosted_backup import _HELPER_LABELS
    record = dict(_base(), phase="backup_intent", old_process_identity={"pid": 4242, "started_at": "started"})
    record["row_identity"] = {"sandbox_id": "sbx", "user_id": "", "organization_id": "", "created_at": ""}
    old = _Container("old", status="paused", writable_home=True)
    old.attrs.update({"Image": record["old_image"], "State": {"Pid": 4242, "StartedAt": "started"},
                      "NetworkSettings": {"Networks": {"bridge": {"Aliases": ["sbx"], "IPAddress": "172.17.0.2/16", "IPPrefixLen": 16, "GlobalIPv6Address": "", "MacAddress": "new"}}}})
    helper = _Container("helper", status="running")
    helper.attrs = {"Image": record["helper_image"], "Mounts": [
        {"Type": "volume", "Name": record["source_volume"], "Destination": "/source", "RW": False},
        {"Type": "volume", "Name": record["backup_name"], "Destination": "/backup", "RW": True},
    ]}
    helper.labels = {**_HELPER_LABELS, "org.opencontainers.image.revision": "image-inherited"}
    class Containers:
        def get(self, identity):
            if identity == "old": return old
            raise NotFound("container", identity)
        def list(self, **_kwargs): return [old, helper]
    client = type("Client", (), {"containers": Containers(), "networks": type("Networks", (), {"get": lambda *_: type("Network", (), {"id": "network-id"})()})()})()
    row = type("Row", (), {"sandbox_id": "sbx", "user_id": "", "organization_id": "", "created_at": "", "container_id": "old"})()
    class Store:
        async def get(self, *_): return row
        async def get_lifecycle(self, *_): return {"status": "ready"}
    resumed = await _recover_pre_copy_source(record, store=Store(), client=client,
                                              journal=type("Journal", (), {"write": lambda *_: None})())
    assert resumed is old and old.unpause_calls == 1
    assert helper.remove_calls == [{"force": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutant", ["labels", "image", "mount"])
async def test_backup_intent_refuses_helper_with_wrong_label_image_or_mount(mutant):
    """Break caught: recovery killed a lookalike container touching home or backup."""
    from orchestrator.hosted_backup import _HELPER_LABELS
    record = dict(_base(), phase="backup_intent")
    helper = _Container("helper", status="running")
    helper.attrs = {"Image": record["helper_image"] if mutant != "image" else "sha256:" + "d" * 64, "Mounts": [
        {"Type": "volume", "Name": record["source_volume"], "Destination": "/source", "RW": False},
        {"Type": "volume", "Name": record["backup_name"], "Destination": "/wrong" if mutant == "mount" else "/backup", "RW": True},
    ]}
    helper.labels = _HELPER_LABELS if mutant != "labels" else {"matrx.kind": "not-backup"}
    client = type("Client", (), {"containers": type("Containers", (), {"list": lambda *_args, **_kwargs: [helper]})()})()
    with pytest.raises(HostedMigrationStateError, match="unrecognized"):
        await _remove_verified_pre_copy_helper(client, record)
    assert helper.remove_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["row", "writer"])
async def test_pre_copy_rechecks_route_and_writer_before_unpause(race):
    """Break caught: original resumed after DB handoff or a sibling gained home write access."""
    from docker.errors import NotFound
    record = dict(_base(), phase="old_stopped", old_process_identity={"pid": 77, "started_at": "started"})
    record["row_identity"] = {"sandbox_id": "sbx", "user_id": "", "organization_id": "", "created_at": ""}
    old = _Container("old", status="paused", writable_home=True)
    old.attrs.update({"Image": record["old_image"], "State": {"Pid": 77, "StartedAt": "started"},
                      "NetworkSettings": {"Networks": {"bridge": {"Aliases": ["sbx"], "IPAddress": "172.17.0.2/16", "IPPrefixLen": 16, "GlobalIPv6Address": "", "MacAddress": "new"}}}})
    sibling = _Container("sibling", status="running", writable_home=True)
    class Containers:
        def get(self, identity):
            if identity == "old": return old
            raise NotFound("container", identity)
        def list(self, **_): return [old, *( [sibling] if race == "writer" else [])]
    client = type("Client", (), {"containers": Containers(), "networks": type("Networks", (), {"get": lambda *_: type("Network", (), {"id": "network-id"})()})()})()
    row = type("Row", (), {"sandbox_id": "sbx", "user_id": "", "organization_id": "", "created_at": "", "container_id": "other" if race == "row" else "old"})()
    class Store:
        async def get(self, *_): return row
        async def get_lifecycle(self, *_): return {"status": "ready"}
    with pytest.raises(HostedMigrationStateError, match="routing changed|another running container"):
        await _recover_pre_copy_source(record, store=Store(), client=client,
                                       journal=type("Journal", (), {"write": lambda *_: None})())
    assert old.unpause_calls == 0


def test_postbackup_precommit_resumes_only_a_paused_source():
    assert recovery_action(dict(_base(), phase="target_created"), db_container_id="old", target_exists_ready=False) == "preserve_fenced"
    assert recovery_action(dict(_base(), phase="target_created", backup_receipt={"schema_version": 1}), db_container_id="old", target_exists_ready=False) == "resume_old"


def test_db_target_match_alone_is_not_commit():
    record = dict(_base(), phase="commit_uncertain", target_id="new", backup_receipt={"x": 1})
    assert recovery_action(record, db_container_id="new", target_exists_ready=False) == "preserve_fenced"
    assert recovery_action(record, db_container_id="new", target_exists_ready=True) == "finalize_committed"


@pytest.mark.asyncio
async def test_held_attestation_waits_for_listener_after_ready_marker(monkeypatch):
    """Regression: async daemon bind after /tmp readiness must not trigger rollback."""
    states = iter(["unavailable", "held"])
    async def state(_container):
        value = next(states)
        if value == "unavailable":
            raise HostedMigrationStateError("target health endpoint did not answer for migration attestation")
        return value
    monkeypatch.setattr("orchestrator.hosted_runtime._migration_state", state)
    await _wait_migration_state(object(), "held", 1)


def test_target_id_must_be_journaled_before_start_intent():
    try:
        transition(_base(), "target_start_intent")
    except HostedMigrationStateError:
        pass
    else:
        raise AssertionError("mutant without target ID was accepted")


def test_only_expected_activity_refusal_is_classified_busy_before_admission():
    assert _migration_failure_status(
        HostedMigrationBusyError("active session"), admitted=False
    ) == "busy_deferred"
    assert _migration_failure_status(
        HostedMigrationStateError("target image missing"), admitted=False
    ) == "failed"
    assert _migration_failure_status(
        HostedMigrationStateError("journaled operation failed"), admitted=True
    ) == "recovery_required"


@pytest.mark.parametrize("phase,expected", [
    ("admitted", "preserve_fenced"), ("old_stopped", "preserve_fenced"),
    ("backup_verified", "resume_old"), ("target_create_intent", "resume_old"),
    ("target_created", "resume_old"), ("target_start_intent", "resume_old"),
    ("target_ready", "resume_old"), ("names_cut_over", "resume_old"),
    ("commit_intent", "resume_old"), ("commit_uncertain", "resume_old"),
    ("committed", "preserve_fenced"), ("recovered", "preserve_fenced"), ("recovery_required", "preserve_fenced"),
])
def test_every_supported_phase_has_a_non_destructive_recovery_decision(phase, expected):
    record = dict(_base(), phase=phase, backup_receipt={"schema_version": 1}, target_id="new")
    assert recovery_action(record, db_container_id="old", target_exists_ready=False) == expected


def test_durable_record_rejects_partial_crash_artifact():
    """Break caught: a valid phase alone drove Docker recovery with missing identities."""
    with pytest.raises(HostedMigrationStateError, match="missing"):
        validate_record({"schema_version": 1, "sandbox_id": "sbx", "phase": "admitted"})


def test_durable_record_accepts_admitted_full_intent():
    validate_record(dict(_base(), phase="admitted"))


def test_historical_schema_v1_committed_journal_without_activation_fields_is_readable(tmp_path):
    """Deployed v1 journals remain recoverable after the v2 writer ships."""
    record = dict(
        _base(), phase="committed", target_id="new",
        backup_receipt={"verified": True}, cleanup_complete=True,
    )
    for field in (
        "activation_home_preflight_receipt", "state_volume_name",
        "state_volume_creation_intent", "helper_image_pin",
        "helper_image_pin_creation_intent", "helper_image_pin_created",
    ):
        record.pop(field, None)
    journal = HostedMigrationJournal(tmp_path)
    journal.write(record)
    assert journal.read("sbx") == record


def test_schema_v2_requires_activation_intents_and_forward_preflight_receipt():
    record = dict(_base(), schema_version=2, phase="admitted")
    with pytest.raises(HostedMigrationStateError, match="schema 2 is missing"):
        validate_record(record)

    record.update(
        state_volume_name="matrx-migration-state-sbx",
        state_volume_creation_intent={"name": "matrx-migration-state-sbx", "sandbox_id": "sbx"},
        helper_image_pin="matrx-migration-helper:op",
        helper_image_pin_creation_intent={
            "pin": "matrx-migration-helper:op", "image": "sha256:" + "c" * 64,
        },
    )
    validate_record(record)
    record.update(
        phase="activation_intent", target_id="new", backup_receipt={"verified": True},
    )
    record.pop("activation_home_preflight_receipt", None)
    with pytest.raises(HostedMigrationStateError, match="lifecycle-path preflight"):
        validate_record(record)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("state_volume_name", None, "state-volume intent"),
        ("state_volume_name", "matrx-migration-state-other", "state-volume intent"),
        ("state_volume_creation_intent", None, "state-volume intent"),
        ("state_volume_creation_intent", {"name": "wrong", "sandbox_id": "sbx"}, "state-volume intent"),
        ("helper_image_pin", None, "helper-pin intent"),
        ("helper_image_pin", "matrx-migration-helper:other", "helper-pin intent"),
        ("helper_image_pin_creation_intent", None, "helper-pin intent"),
        ("helper_image_pin_creation_intent", {"pin": "wrong", "image": "sha256:" + "c" * 64}, "helper-pin intent"),
    ],
)
def test_schema_v2_rejects_null_or_unbound_resource_intents(field, value, reason):
    record = dict(
        _base(), schema_version=2, phase="admitted",
        state_volume_name="matrx-migration-state-sbx",
        state_volume_creation_intent={"name": "matrx-migration-state-sbx", "sandbox_id": "sbx"},
        helper_image_pin="matrx-migration-helper:op",
        helper_image_pin_creation_intent={
            "pin": "matrx-migration-helper:op", "image": "sha256:" + "c" * 64,
        },
    )
    record[field] = value
    with pytest.raises(HostedMigrationStateError, match=reason):
        validate_record(record)


def test_bind_mount_state_guard_uses_mountinfo_even_when_device_identity_is_shared():
    """Break caught: EC2 bind-mounted journal state was rejected by os.path.ismount()."""
    target = "/var/lib/matrx-sandbox/hosted-migrations"
    bind_mountinfo = (
        "991 23 259:1 /var/lib/matrx-sandbox/hosted-migrations " + target +
        " rw,relatime - xfs /dev/nvme0n1p1 rw\n"
    )
    sibling_mountinfo = "991 23 259:1 /var/lib/matrx-sandbox/other /var/lib/matrx-sandbox/other rw - xfs /dev/nvme0n1p1 rw\n"
    assert _mountinfo_has_mountpoint(bind_mountinfo, Path(target))
    assert not _mountinfo_has_mountpoint(sibling_mountinfo, Path(target))


@pytest.mark.parametrize("phase", ["rollback_intent", "restore_intent"])
def test_verified_backup_rollback_can_be_durable_before_target_exists(phase):
    """Break caught: recovery cannot persist its restore intent before target creation."""
    validate_record(
        dict(_base(), phase=phase, backup_receipt={"verified": True})
    )


@pytest.mark.parametrize("phase", ["target_created", "target_start_intent", "commit_intent"])
def test_target_phase_still_refuses_missing_target_identity(phase):
    with pytest.raises(HostedMigrationStateError, match="target identity"):
        validate_record(dict(_base(), phase=phase, backup_receipt={"verified": True}))


def test_ec2_postboot_and_activation_phases_require_their_durable_receipts():
    """Break caught: CAS/recovery must not infer postboot verification or activation."""
    record = dict(
        _base(), phase="postboot_verified", target_id="new", backup_receipt={"verified": True, "manifest_sha256": "digest"},
        storage_kind="ec2_writable_layer", source_home_key="layer-sbx", source_graph_driver={},
        source_volume="matrx-ec2-home-sbx", source_identity={"name": "matrx-ec2-home-sbx"},
    )
    with pytest.raises(HostedMigrationStateError, match="postboot"):
        validate_record(record)
    record["postboot_verified_receipt"] = {
        "ok": True, "operation": "op", "target_id": "new",
        "target_image": "sha256:" + "b" * 64, "manifest_sha256": "digest",
    }
    validate_record(record)
    record["phase"] = "committed"
    with pytest.raises(HostedMigrationStateError, match="active-target"):
        validate_record(record)
    record["activation_receipt"] = {"target_id": "new", "migration_state": "active"}
    validate_record(record)


@pytest.mark.parametrize(
    ("phase", "cleanup_complete", "expected"),
    [
        ("admitted", False, {"old", "new"}),
        ("committed", False, {"old"}),
        ("recovered", False, {"new"}),
        ("committed", True, set()),
    ],
)
def test_retained_artifacts_exclude_the_current_runtime(tmp_path, phase, cleanup_complete, expected):
    """Break caught: reconcile hides the usable committed/recovered container."""
    journal = HostedMigrationJournal(tmp_path)
    record = dict(_base(), phase=phase, cleanup_complete=cleanup_complete)
    if phase != "recovered":
        record.update(target_id="new", backup_receipt={"verified": True})
    else:
        record.update(target_id="new")
    journal.write(record)
    assert journal.retained_container_ids() == expected


class _Container:
    def __init__(self, identity, *, status="exited", writable_home=False):
        self.id, self.status = identity, status
        self.attrs = {"Mounts": [{"Type": "volume", "Name": "home", "Destination": "/home/agent", "RW": writable_home}]}
        self.stop_calls = 0
        self.unpause_calls = 0
        self.pause_calls = 0
        self.remove_calls = []

    def reload(self):
        return None

    def stop(self, **_):
        self.stop_calls += 1
        self.status = "exited"

    def unpause(self):
        self.unpause_calls += 1
        self.status = "running"

    def pause(self):
        self.pause_calls += 1
        self.status = "paused"

    def remove(self, **kwargs):
        self.remove_calls.append(kwargs)


def test_exited_old_source_is_fenced_before_target_or_home_cleanup():
    """Break caught: rollback restarted an exited original and changed its user home."""
    with pytest.raises(HostedMigrationStateError, match="not paused"):
        _require_paused_source(_Container("old", status="exited"))


@pytest.mark.parametrize("status", ["created", "exited", "dead", "paused"])
def test_created_or_quiesced_target_can_be_exactly_removed_without_shutdown(status):
    """Break caught: rollback required a pause even when a target never ran."""
    _require_rollback_safe_target(_Container("new", status=status))


def test_unpaused_target_is_not_safe_until_the_recovery_quiesce_step_runs():
    """Break caught: a live target was removed/rolled back without a stable home proof."""
    with pytest.raises(HostedMigrationStateError, match="target is running"):
        _require_rollback_safe_target(_Container("new", status="running"))


@pytest.mark.asyncio
async def test_running_target_is_paused_before_rollback_manifest_or_remove():
    """Break caught: a routine target-ready failure remained fenced instead of safely quiescing."""
    class Journal:
        def __init__(self): self.writes = []
        def write(self, record): self.writes.append(dict(record))
    target, journal = _Container("new", status="running"), Journal()
    record = dict(
        _base(), schema_version=2, phase="target_start_intent", target_id="new",
        backup_receipt={"verified": True},
        state_volume_name="matrx-migration-state-sbx",
        state_volume_creation_intent={"name": "matrx-migration-state-sbx", "sandbox_id": "sbx"},
        helper_image_pin="matrx-migration-helper:op",
        helper_image_pin_creation_intent={
            "pin": "matrx-migration-helper:op", "image": "sha256:" + "c" * 64,
        },
    )
    record.pop("activation_home_preflight_receipt", None)
    await _pause_target_for_rollback(record, target, journal)
    assert target.pause_calls == 1 and target.status == "paused"
    assert journal.writes[-1]["phase"] == "rollback_quiesce_intent"


@pytest.mark.asyncio
async def test_aidream_held_readiness_does_not_require_post_commit_managed_api():
    """Break caught: Aidream held target waited for :8001, which starts only after commit."""
    class HeldAidream:
        status = "running"
        commands = []

        def reload(self):
            return None

        def exec_run(self, command):
            self.commands.append(command)
            script = command[-1] if isinstance(command, list) else command
            if "127.0.0.1:8001" in script:
                return 1, b"managed API intentionally held"
            return 0, b""

    target = HeldAidream()
    assert await _wait_container_ready(target, 1, "aidream", stage="held")
    assert target.commands[0][-3:-1] == ["/bin/sh", "-ec"]
    assert "127.0.0.1:8000/health" in target.commands[0][-1]
    assert "aidream-helpers.sh verify-release" in target.commands[0][-1]
    assert "127.0.0.1:8001" not in target.commands[0][-1]


@pytest.mark.asyncio
async def test_aidream_active_readiness_still_requires_managed_api():
    """Break caught: held/core readiness was accidentally reused as post-CAS success."""
    class DegradedAidream:
        status = "running"

        def reload(self):
            return None

        def exec_run(self, command):
            script = command[-1] if isinstance(command, list) else command
            return (1, b"managed API unavailable") if "127.0.0.1:8001" in script else (0, b"")

    assert not await _wait_container_ready(DegradedAidream(), 1, "aidream", stage="active")


@pytest.mark.asyncio
async def test_aidream_rollback_readiness_restores_core_without_claiming_managed_api():
    """Break caught: a pre-existing :8001 failure stranded an exactly resumed old process."""
    class BaselineDegradedAidream:
        status = "running"
        commands = []

        def reload(self):
            return None

        def exec_run(self, command):
            self.commands.append(command)
            return 0, b""

    old = BaselineDegradedAidream()
    assert await _wait_container_ready(old, 1, "aidream", stage="rollback")
    assert old.commands[0][-3:-1] == ["/bin/sh", "-ec"]
    assert "127.0.0.1:8000/health" in old.commands[0][-1]
    assert "127.0.0.1:8001" not in old.commands[0][-1]


@pytest.mark.asyncio
async def test_aidream_template_probe_uses_explicit_shell_for_redirection():
    """Break caught: docker-py split `>/dev/null` into a literal curl argument."""
    from orchestrator.hosted_runtime import _template_service_ready

    class Probe:
        command = None

        def exec_run(self, command):
            self.command = command
            return (0, b"") if isinstance(command, list) else (2, b"curl: bad argument")

    container = Probe()
    assert await _template_service_ready(container, "aidream") is True
    assert container.command == [
        "curl", "-fsS", "--max-time", "3",
        "http://127.0.0.1:8001/api/health/ready",
    ]


@pytest.mark.asyncio
async def test_readiness_timeout_is_a_wall_clock_deadline(monkeypatch):
    """Break caught: slow probes were excluded from the configured readiness budget."""
    from orchestrator import migrate

    clock = [100.0]
    sleeps = []

    class SlowFailure:
        status = "running"

        def reload(self):
            return None

        def exec_run(self, _command):
            clock[0] += 0.8
            return 1, b"not ready"

    async def advance(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(migrate.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(migrate.asyncio, "sleep", advance)
    assert not await _wait_container_ready(SlowFailure(), 1, "aidream", stage="active")
    assert sleeps == [pytest.approx(0.2)]


@pytest.mark.asyncio
async def test_live_shape_target_start_recovery_quiesces_then_resumes_baseline_degraded_old(
    monkeypatch,
):
    """Break caught live: preflight-gated quiesce stranded the paused old Aidream box."""
    record = dict(
        _base(), schema_version=2, phase="target_start_intent", target_id="new",
        template="aidream", backup_receipt={"verified": True}, row_persistence_volume=None,
        old_process_identity={"pid": 4242, "started_at": "started"},
        state_volume_name="matrx-migration-state-sbx",
        state_volume_creation_intent={"name": "matrx-migration-state-sbx", "sandbox_id": "sbx"},
        helper_image_pin="matrx-migration-helper:op",
        helper_image_pin_creation_intent={
            "pin": "matrx-migration-helper:op", "image": "sha256:" + "c" * 64,
        },
    )
    record.pop("activation_home_preflight_receipt", None)
    old = _Container("old", status="paused", writable_home=True)
    old.name = "/sbx"
    old.attrs.update({
        "Image": record["old_image"],
        "State": {"Pid": 4242, "StartedAt": "started"},
    })
    target = _Container("new", status="running", writable_home=True)
    row = type("Row", (), {"container_id": "old", "persistence_volume": None})()

    async def current(*_args, **_kwargs): return row
    async def get_container(_client, identity): return old if identity == "old" else None
    async def no_op(*_args, **_kwargs): return None
    async def ready(container, _record, *, target): return container is old and not target
    async def template_ready(*_args, **_kwargs): return False
    cleaned = []

    async def cleanup(value, _client, journal, **_kwargs):
        cleaned.append(True)
        value["cleanup_complete"] = True
        journal.write(value)

    monkeypatch.setattr("orchestrator.hosted_runtime._current", current)
    monkeypatch.setattr("orchestrator.hosted_runtime._target", lambda *_args, **_kwargs: asyncio.sleep(0, result=target))
    monkeypatch.setattr("orchestrator.hosted_runtime._get", get_container)
    monkeypatch.setattr("orchestrator.hosted_runtime._assert_no_unowned_home_writer", no_op)
    monkeypatch.setattr("orchestrator.hosted_runtime._resolve_disconnect_intent", no_op)
    monkeypatch.setattr("orchestrator.hosted_runtime._reconnect_paused_source", no_op)
    monkeypatch.setattr("orchestrator.hosted_runtime._ensure_rollback_home", no_op)
    monkeypatch.setattr("orchestrator.hosted_runtime._ready", ready)
    monkeypatch.setattr("orchestrator.hosted_runtime._template_service_ready", template_ready)
    monkeypatch.setattr("orchestrator.hosted_runtime._cleanup", cleanup)
    monkeypatch.setattr("orchestrator.hosted_backup._volume_identity", lambda *_: record["source_identity"])

    volume = type("Volume", (), {"attrs": {}})()
    client = type("Client", (), {"volumes": type("Volumes", (), {"get": lambda *_: volume})()})()

    class Journal:
        def __init__(self): self.writes = []
        def write(self, value): self.writes.append(dict(value))

    journal = Journal()
    result = await recover_hosted_migration(
        record, store=object(), client=client, journal=journal, locked=True,
    )
    assert result == {"status": "recovered", "sandbox_id": "sbx", "baseline_unknown": True}
    assert target.pause_calls == 1 and target.remove_calls == [{"force": True}]
    assert old.unpause_calls == 1 and cleaned == [True]
    assert any(item["phase"] == "rollback_quiesce_intent" for item in journal.writes)
    assert journal.writes[-1]["cleanup_complete"] is True


@pytest.mark.asyncio
async def test_changed_held_home_restores_verified_backup_before_rollback(monkeypatch):
    """Break caught live: target drift must recover, not strand both containers paused."""
    from orchestrator.hosted_backup import HostedBackupError

    verified = {"manifest_sha256": "restored"}
    checks = iter([
        HostedBackupError("shared home manifest changed while migration target was held"),
        verified,
    ])

    async def verify(*_args, **_kwargs):
        result = next(checks)
        if isinstance(result, Exception):
            raise result
        return result

    restored = []

    async def restore(*_args, **_kwargs):
        restored.append(True)

    monkeypatch.setattr("orchestrator.hosted_backup.verify_volume_unchanged", verify)
    monkeypatch.setattr("orchestrator.hosted_backup.restore_volume", restore)
    record = dict(_base(), phase="target_quiesce_intent", backup_receipt={"verified": True})

    class Journal:
        def __init__(self): self.writes = []
        def write(self, value): self.writes.append(dict(value))

    journal = Journal()
    assert await _ensure_rollback_home(record, object(), journal) == verified
    assert restored == [True]
    assert any(item["phase"] == "restore_intent" for item in journal.writes)
    assert record["rollback_home_receipt"] == verified


@pytest.mark.asyncio
async def test_rollback_never_restores_for_unclassified_backup_failure(monkeypatch):
    """Missing or corrupt evidence stays fenced before any writable helper."""
    from unittest.mock import AsyncMock
    from orchestrator.hosted_backup import HostedBackupError

    async def verify(*_args, **_kwargs):
        raise HostedBackupError("backup archive does not match its verified receipt")

    restore = AsyncMock()
    monkeypatch.setattr("orchestrator.hosted_backup.verify_volume_unchanged", verify)
    monkeypatch.setattr("orchestrator.hosted_backup.restore_volume", restore)
    record = dict(_base(), phase="target_quiesce_intent", backup_receipt={"verified": True})
    journal = type("Journal", (), {"write": lambda *_: None})()

    with pytest.raises(HostedBackupError, match="archive"):
        await _ensure_rollback_home(record, object(), journal)
    restore.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_held_target_cannot_bypass_home_restore_before_old_resumes(monkeypatch):
    """A vanished drifted target must not let recovery unpause changed home."""
    from orchestrator.hosted_backup import HostedBackupError

    restored = False
    checks = 0
    record = dict(
        _base(),
        phase="target_quiesce_intent",
        target_id="new",
        backup_receipt={"verified": True},
        row_persistence_volume=None,
    )
    old = _Container("old", status="paused", writable_home=True)
    old.name = "/sbx"
    old.attrs["Image"] = record["old_image"]

    def unpause():
        assert restored, "old resumed before the shared home was restored"
        old.unpause_calls += 1
        old.status = "running"

    old.unpause = unpause
    row = type("Row", (), {"container_id": "old", "persistence_volume": None})()

    async def current(*_args, **_kwargs): return row
    async def missing_target(*_args, **_kwargs): return None
    async def no_writer(*_args, **_kwargs): return None
    async def get_old(*_args, **_kwargs): return old
    async def no_disconnect(*_args, **_kwargs): return None
    async def ready(*_args, **_kwargs): return True
    async def cleanup(*_args, **_kwargs): return None

    async def verify(*_args, **_kwargs):
        nonlocal checks
        checks += 1
        if checks == 1:
            raise HostedBackupError("shared home manifest changed while migration target was held")
        return {"manifest_sha256": "restored"}

    async def restore(*_args, **_kwargs):
        nonlocal restored
        restored = True

    monkeypatch.setattr("orchestrator.hosted_runtime._current", current)
    monkeypatch.setattr("orchestrator.hosted_runtime._target", missing_target)
    monkeypatch.setattr("orchestrator.hosted_runtime._assert_no_unowned_home_writer", no_writer)
    monkeypatch.setattr("orchestrator.hosted_runtime._get", get_old)
    monkeypatch.setattr("orchestrator.hosted_runtime._resolve_disconnect_intent", no_disconnect)
    monkeypatch.setattr("orchestrator.hosted_runtime._reconnect_paused_source", no_disconnect)
    monkeypatch.setattr("orchestrator.hosted_runtime._ready", ready)
    monkeypatch.setattr("orchestrator.hosted_runtime._cleanup", cleanup)
    monkeypatch.setattr("orchestrator.hosted_backup._volume_identity", lambda *_: record["source_identity"])
    monkeypatch.setattr("orchestrator.hosted_backup.verify_volume_unchanged", verify)
    monkeypatch.setattr("orchestrator.hosted_backup.restore_volume", restore)

    volume = type("Volume", (), {"attrs": {}})()
    client = type("Client", (), {"volumes": type("Volumes", (), {"get": lambda *_: volume})()})()

    class Journal:
        def __init__(self): self.writes = []
        def write(self, value): self.writes.append(dict(value))

    result = await recover_hosted_migration(
        record,
        store=object(),
        client=client,
        journal=Journal(),
        locked=True,
    )
    assert result == {"status": "recovered", "sandbox_id": "sbx"}
    assert restored and checks == 2 and old.unpause_calls == 1


@pytest.mark.asyncio
async def test_unpausable_target_remains_fenced_before_manifest_or_remove():
    """Break caught: recovery continued after Docker failed to pause a live target."""
    class Unpausable(_Container):
        def pause(self): self.pause_calls += 1
    target = Unpausable("new", status="running")
    with pytest.raises(HostedMigrationStateError, match="did not pause"):
        await _pause_target_for_rollback(dict(_base(), phase="target_ready", target_id="new", backup_receipt={"verified": True}), target, type("Journal", (), {"write": lambda *_: None})())


class _ContainerList:
    def __init__(self, containers):
        self._containers = containers

    def list(self, **_):
        return self._containers


@pytest.mark.asyncio
async def test_recovery_force_removes_an_already_exited_target():
    """Break caught: rollback called image shutdown hooks before exact target removal."""
    target = _Container("new", status="exited", writable_home=True)
    await _quiesce_target(target, {"stop_timeout": 1})
    assert target.stop_calls == 0 and target.remove_calls == [{"force": True}]


@pytest.mark.asyncio
async def test_recovery_force_removes_a_held_target_without_unpausing():
    """Break caught: unpausing a held target ran shutdown/startup writes on shared home."""
    target = _Container("new", status="paused", writable_home=True)
    await _quiesce_target(target, {"stop_timeout": 1})
    assert target.unpause_calls == 0 and target.stop_calls == 0
    assert target.remove_calls == [{"force": True}]


@pytest.mark.asyncio
async def test_promoted_activation_waits_for_active_attestation_and_advances_shared_record(monkeypatch, tmp_path):
    """Break caught: a held HTTP-200 target was committed/cleaned before its daemon activated."""
    journal = HostedMigrationJournal(tmp_path)
    store = InMemorySandboxStore()
    user_id = "00000000-0000-4000-8000-000000000001"
    organization_id = "22222222-2222-4222-8222-222222222222"
    row = SandboxResponse(
        sandbox_id="sbx", user_id=user_id, organization_id=organization_id, status=SandboxStatus.READY,
        container_id="new", persistence_volume="matrx-ec2-home-sbx", created_at=datetime.now(timezone.utc),
    )
    await store.save(row)
    record = dict(
        _base(), phase="commit_intent", target_id="new", backup_receipt={"verified": True, "manifest_sha256": "digest"},
        storage_kind="ec2_writable_layer", source_home_key="layer-sbx", source_graph_driver={},
        source_volume="matrx-ec2-home-sbx", source_identity={"name": "matrx-ec2-home-sbx"},
        postboot_verified_receipt={
            "ok": True, "operation": "op", "target_id": "new",
            "target_image": "sha256:" + "b" * 64, "manifest_sha256": "digest",
        }, verify_timeout=2,
    )
    record["row_identity"] = {"sandbox_id": "sbx", "user_id": user_id, "organization_id": organization_id, "created_at": str(row.created_at)}
    journal.write(record)

    class Target:
        id = "new"
        status = "paused"
        attrs = {"Mounts": [{"Type": "volume", "Name": "matrx-ec2-home-sbx", "Destination": "/home/agent", "RW": True}]}
        def reload(self): pass
        def unpause(self): self.status = "running"
        def exec_run(self, _command): return (0, b"")

    states = iter(["held", "held", "active"])
    async def state(_target): return next(states)
    async def ready(*_args, **_kwargs): return True
    async def cleanup(*_args, **_kwargs): return None
    monkeypatch.setattr("orchestrator.hosted_runtime._migration_state", state)
    monkeypatch.setattr("orchestrator.hosted_runtime._ready", ready)
    monkeypatch.setattr("orchestrator.hosted_runtime._cleanup", cleanup)

    await _activate_promoted_target(record, target=Target(), store=store, client=object(), journal=journal)
    assert record["phase"] == "committed"
    assert record["activation_receipt"] == {"target_id": "new", "migration_state": "active"}
    _record_error(record, journal, RuntimeError("after activation"))
    assert journal.read("sbx")["phase"] == "committed"


@pytest.mark.asyncio
async def test_promoted_activation_refuses_conflicting_persisted_home_before_unpause(tmp_path):
    """Break caught: DB target ID alone must not activate a target on another home."""
    journal, store = HostedMigrationJournal(tmp_path), InMemorySandboxStore()
    row = SandboxResponse(sandbox_id="sbx", user_id="00000000-0000-4000-8000-000000000001", organization_id="22222222-2222-4222-8222-222222222222", status=SandboxStatus.READY,
        container_id="new", persistence_volume="wrong-home", created_at=datetime.now(timezone.utc))
    await store.save(row)
    record = dict(_base(), phase="commit_intent", target_id="new", source_volume="expected-home",
        source_identity={"name": "expected-home"}, storage_kind="ec2_writable_layer",
        source_home_key="layer-sbx", source_graph_driver={}, backup_receipt={"manifest_sha256": "digest"},
            postboot_verified_receipt={"ok": True, "operation": "op", "target_id": "new",
                "target_image": "sha256:" + "b" * 64, "manifest_sha256": "digest"})
    record["row_identity"] = {"sandbox_id": "sbx", "user_id": row.user_id,
                              "organization_id": row.organization_id, "created_at": str(row.created_at)}
    class Target:
        id, status, unpause_calls = "new", "paused", 0
        def reload(self): pass
        def unpause(self): self.unpause_calls += 1
    target = Target()
    with pytest.raises(HostedMigrationStateError, match="home routing"):
        await _activate_promoted_target(record, target=target, store=store, client=object(), journal=journal)
    assert target.unpause_calls == 0


@pytest.mark.asyncio
async def test_hosted_activation_preserves_legacy_null_row_home_while_validating_named_mount(monkeypatch, tmp_path):
    """Break caught: hosted CAS rewrote legacy NULL persistence_volume to a physical Docker volume."""
    journal, store = HostedMigrationJournal(tmp_path), InMemorySandboxStore()
    row = SandboxResponse(sandbox_id="sbx", user_id="00000000-0000-4000-8000-000000000001", organization_id="22222222-2222-4222-8222-222222222222", status=SandboxStatus.READY,
        container_id="new", persistence_volume=None, created_at=datetime.now(timezone.utc))
    await store.save(row)
    record = dict(_base(), phase="commit_intent", target_id="new", backup_receipt={"verified": True},
                  source_volume="matrx-user-physical", source_identity={"name": "matrx-user-physical"},
                  row_persistence_volume=None)
    record["row_identity"] = {"sandbox_id": "sbx", "user_id": row.user_id,
                              "organization_id": row.organization_id, "created_at": str(row.created_at)}
    class Target:
        id, status = "new", "paused"
        attrs = {"Mounts": [{"Type": "volume", "Name": "matrx-user-physical", "Destination": "/home/agent", "RW": True}]}
        def reload(self): pass
        def unpause(self): self.status = "running"
        def exec_run(self, _): return (0, b"")
    async def ready(*_args, **_kwargs): return True
    async def state(_target): return "active"
    async def cleanup(*_args, **_kwargs): return None
    monkeypatch.setattr("orchestrator.hosted_runtime._ready", ready)
    monkeypatch.setattr("orchestrator.hosted_runtime._migration_state", state)
    monkeypatch.setattr("orchestrator.hosted_runtime._cleanup", cleanup)
    await _activate_promoted_target(record, target=Target(), store=store, client=object(), journal=journal)
    assert (await store.get("sbx")).persistence_volume is None


@pytest.mark.asyncio
async def test_recovery_refuses_a_running_sibling_writer_before_restore():
    """Break caught: restoring a shared home overwrites a sibling's live write."""
    sibling = _Container("sibling", status="running", writable_home=True)
    client = type("Client", (), {"containers": _ContainerList([sibling])})()
    with pytest.raises(HostedMigrationStateError, match="another running container"):
        await _assert_no_unowned_home_writer(client, _base(), allowed_ids={"old", "new"})


@pytest.mark.asyncio
async def test_pre_admission_sibling_writer_is_actionable_busy_control_flow():
    """A safe shared-home refusal is not a red unknown migration failure."""
    sibling = _Container("sibling", status="running", writable_home=True)
    client = type("Client", (), {"containers": _ContainerList([sibling])})()
    with pytest.raises(HostedMigrationBusyError, match="stop that sandbox"):
        await _assert_migration_home_exclusive(client, _base(), allowed_ids={"old"})


def test_missing_hosted_journal_fails_closed(monkeypatch):
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "hosted")
    assert hosted_fenced("sbx")
    assert hosted_volume_fenced("matrx-user-u")


def test_forward_phase_rejects_false_lifecycle_preflight_receipt():
    record = dict(_base(), phase="activation_intent", target_id="new", backup_receipt={"verified": True})
    record["state_volume_name"] = "matrx-migration-state-sbx"
    record["state_volume_creation_intent"] = {
        "name": "matrx-migration-state-sbx", "sandbox_id": "sbx",
    }
    record["activation_home_preflight_receipt"] = {
        "target_id": "new", "agent_lifecycle_paths_writable": False,
    }
    with pytest.raises(HostedMigrationStateError, match="lifecycle-path preflight"):
        validate_record(record)


def test_ec2_missing_journal_fails_closed(monkeypatch):
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "ec2")
    assert hosted_fenced("sbx")
    assert hosted_volume_fenced("matrx-user-u")


def test_ec2_real_journal_allows_clean_home_then_fences_pending_migration(monkeypatch, tmp_path):
    """Break caught: EC2 must not treat an unavailable/pending journal as permission."""
    journal = HostedMigrationJournal(tmp_path)
    monkeypatch.setattr("orchestrator.config.settings.host_tier", "ec2")
    monkeypatch.setattr("orchestrator.hosted_migration.HostedMigrationJournal", lambda: journal)
    assert not hosted_fenced("sbx")
    assert not hosted_volume_fenced("matrx-user-u")

    journal.write(dict(
        _base(), phase="admitted", source_volume="matrx-user-u",
        source_identity={"name": "matrx-user-u"},
    ))
    assert hosted_fenced("sbx")
    assert hosted_volume_fenced("matrx-user-u")
